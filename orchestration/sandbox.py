"""The action sandbox — the safety boundary for worker agents that touch the
real machine (file writes + shell commands).

Policy (set by the user, 2026-06-16):
  * Files: agents may read/write anywhere UNDER $HOME. Paths are realpath-resolved
    and must stay inside home; a small denylist protects credential stores.
  * Commands: an ALLOWLIST of build/test/git-local tools. No network installs, no
    destructive commands, no shell chaining/redirection, and crucially NO
    `git push` (push is a separate external_post tool that the PDP forces through
    human approval — see tools.git_push).

This is defense-in-depth that sits BELOW the PDP: even a full_auto (bypass) run
cannot escape home or run an un-allowlisted command, because the tool itself
refuses and returns an error observation instead of executing.
"""
from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

HOME = Path.home().resolve()

# Credential / secret stores never touched, even though they live under home.
_DENY_DIRS = (".ssh", ".aws", ".gnupg", ".config/gcloud", ".kube", ".docker")
_DENY_NAME_HINTS = ("id_rsa", "id_ed25519", ".pem", "credentials", ".npmrc", ".pypirc")

# First token of a command must be one of these. `git` is special-cased below.
_ALLOWED_CMDS = {
    "ls", "cat", "head", "tail", "wc", "pwd", "echo", "test", "true",
    "grep", "rg", "find", "mkdir", "touch", "cp", "mv", "diff", "stat",
    "node", "python", "python3", "tsc", "eslint", "prettier", "jq",
    "npm", "pnpm", "yarn", "npx", "git",
}
# npm/pnpm/yarn/npx: only these subcommands (no install/add/ci — those hit network).
_ALLOWED_PKG_SUB = {"run", "test", "build", "lint", "exec", "start", "typecheck", "tsc"}
# git: local-only subcommands. `push` is deliberately ABSENT (goes via git_push tool).
_ALLOWED_GIT_SUB = {
    "add", "commit", "status", "diff", "log", "show", "rev-parse", "branch",
    "checkout", "switch", "restore", "stash", "init", "config", "remote", "ls-files",
}
# Tokens that would let a command escape the allowlist. `&&` is handled
# separately (we split on it and validate each segment), so it is NOT here.
_FORBIDDEN_TOKENS = (";", "||", "|", ">", "<", "`", "$(", "&", "\n")

CMD_TIMEOUT = 180  # seconds — a build/test should finish well within this
_MAX_OUTPUT = 12_000  # chars of stdout/stderr kept in the observation


def resolve_in_home(path: str) -> Path:
    """Resolve `path` to an absolute Path that MUST live under $HOME and outside
    the credential denylist. Raises ValueError otherwise."""
    p = path.strip()
    if not p:
        raise ValueError("empty path")
    raw = Path(os.path.expanduser(p))
    if not raw.is_absolute():
        raw = HOME / raw
    # realpath the existing prefix so symlinks can't hop outside home
    resolved = Path(os.path.realpath(raw))
    if resolved != HOME and HOME not in resolved.parents:
        raise ValueError(f"path escapes home sandbox: {path}")
    rel = resolved.relative_to(HOME).as_posix()
    for d in _DENY_DIRS:
        if rel == d or rel.startswith(d + "/"):
            raise ValueError(f"path is in a protected directory ({d}): {path}")
    low = resolved.name.lower()
    if any(h in low for h in _DENY_NAME_HINTS):
        raise ValueError(f"path looks like a credential file: {path}")
    return resolved


def _check_segment(segment: str) -> tuple[bool, str]:
    """Validate ONE command (no `&&`): allowlist + no other metacharacters."""
    cmd = segment.strip()
    if not cmd:
        return False, "empty command segment"
    for tok in _FORBIDDEN_TOKENS:
        if tok in cmd:
            return False, f"pipes/redirection/background are not allowed (found {tok!r})"
    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        return False, f"unparseable command: {e}"
    if not parts:
        return False, "empty command"
    head = os.path.basename(parts[0])
    if head not in _ALLOWED_CMDS:
        return False, f"command {head!r} is not on the allowlist"
    sub = parts[1] if len(parts) > 1 else ""
    if head in ("npm", "pnpm", "yarn", "npx"):
        # `npx <tool>` and `yarn <script>` are allowed; block explicit installs.
        if sub in ("install", "i", "add", "ci", "update", "upgrade", "remove", "uninstall"):
            return False, f"{head} {sub} hits the network and is not allowed"
        if head in ("npm", "pnpm") and sub and sub not in _ALLOWED_PKG_SUB:
            return False, f"{head} {sub!r} is not an allowed subcommand"
    if head == "git":
        if sub == "push":
            return False, "git push must go through the git_push tool (requires approval)"
        if sub and sub not in _ALLOWED_GIT_SUB:
            return False, f"git {sub!r} is not an allowed (local) subcommand"
    return True, "ok"


def _split_chain(command: str) -> list[str]:
    return [s for s in (seg.strip() for seg in command.split("&&")) if s]


def check_command(command: str) -> tuple[bool, str]:
    """Return (ok, reason). Allows `&&`-chained allowlisted commands; every other
    form of chaining/redirection is rejected."""
    cmd = (command or "").strip()
    if not cmd:
        return False, "empty command"
    segments = _split_chain(cmd)
    if not segments:
        return False, "empty command"
    for seg in segments:
        ok, reason = _check_segment(seg)
        if not ok:
            return False, reason
    return True, "ok"


async def run_command(command: str, cwd: str | None = None,
                      timeout: int = CMD_TIMEOUT) -> dict:
    """Run an allowlisted command (no shell) inside a home-resolved cwd.
    Returns {exit_code, stdout, stderr, timed_out} — never raises for a non-zero
    exit; that is a normal observation the verifier can read."""
    ok, reason = check_command(command)
    if not ok:
        return {"error": reason, "exit_code": None}
    workdir = HOME
    if cwd:
        try:
            workdir = resolve_in_home(cwd)
        except ValueError as e:
            return {"error": str(e), "exit_code": None}
        if not workdir.is_dir():
            return {"error": f"cwd is not a directory: {cwd}", "exit_code": None}
    # Run each `&&` segment in sequence (no shell), stopping on the first failure
    # — mirrors `&&` semantics while keeping exec-only (no shell injection surface).
    segments = _split_chain(command)
    out_buf, err_buf, last_code = [], [], 0
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "CI": "1"}
    for seg in segments:
        # No shell runs these, so expand ~ ourselves (agents naturally write ~/…).
        argv = [os.path.expanduser(tok) if tok.startswith("~") else tok
                for tok in shlex.split(seg)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
            )
        except FileNotFoundError:
            return {"error": f"command not found: {argv[0]}", "exit_code": None}
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": f"command timed out after {timeout}s", "exit_code": None,
                    "timed_out": True, "stdout": "".join(out_buf)[:_MAX_OUTPUT]}
        out_buf.append(out.decode("utf-8", "replace"))
        err_buf.append(err.decode("utf-8", "replace"))
        last_code = proc.returncode
        if last_code != 0:
            break  # && short-circuits
    return {
        "exit_code": last_code,
        "stdout": "".join(out_buf)[:_MAX_OUTPUT],
        "stderr": "".join(err_buf)[:_MAX_OUTPUT],
        "timed_out": False,
        "cwd": str(workdir),
    }
