"""NotifyPort — how CurlyOS-Core reaches a human, without knowing how.

The contract (curlyos-final/07 §2, Port 3): core calls `notify(text, ...)`;
an adapter delivers it. Core never imports anything Hermes/Telegram-shaped —
delete Hermes and notifications degrade to the log + the webapp surfaces,
nothing else changes (P1 replaceability).

Implementations:
  NullNotifier   — logs the notification; always available; the default.
  HermesNotifier — shells out to `hermes send` (the Hermes CLI's ops surface:
                   no LLM, no agent loop, reuses the gateway's platform
                   credentials). Failure is logged, never raised — a
                   notification is never worth blocking cognition for.

Selection: CURLYOS_NOTIFIER env ("null" default | "hermes"), checked in the
process env first, then curlyos-core's own .env file (systemd units don't
load .env; the file is the no-sudo configuration path).
CURLYOS_NOTIFY_TARGET picks the `hermes send -t` target (default "telegram",
the home channel; e.g. "telegram:#OS Updates" for a group topic).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("curlyos-core.notify")


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.partition("=")[2].strip().strip('"').strip("'")
    except OSError:
        pass
    return default


class Notifier:
    """Abstract notifier. Returns True when the message was actually delivered
    to an external surface (False = logged only)."""

    async def notify(self, text: str, *, approval_id: str | None = None,
                     run_id: str | None = None) -> bool:
        raise NotImplementedError


class NullNotifier(Notifier):
    """Log-only delivery — the always-works floor."""

    async def notify(self, text: str, *, approval_id: str | None = None,
                     run_id: str | None = None) -> bool:
        refs = " ".join(f"{k}={v}" for k, v in
                        (("approval", approval_id), ("run", run_id)) if v)
        log.info("NOTIFY%s: %s", f" [{refs}]" if refs else "", text)
        return False


class HermesNotifier(Notifier):
    """Delivery via `hermes send` — Telegram (or any platform Hermes has
    credentials for). Core never touches the platform credentials (P1)."""

    def __init__(self, target: str | None = None, binary: str = "hermes") -> None:
        self.target = target or _env("CURLYOS_NOTIFY_TARGET", "telegram")
        self.binary = binary

    async def notify(self, text: str, *, approval_id: str | None = None,
                     run_id: str | None = None) -> bool:
        refs = " ".join(f"[{k}:{v}]" for k, v in
                        (("approval", approval_id), ("run", run_id)) if v)
        body = f"{text}\n{refs}" if refs else text
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "send", "-t", self.target, "-s", "CurlyOS", "-q", body,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=20)
            except asyncio.TimeoutError:
                proc.kill()
                log.warning("hermes send timed out — notification dropped to log: %s", text)
                return False
            if proc.returncode == 0:
                return True
            log.warning("hermes send failed rc=%s (%s) — notification: %s",
                        proc.returncode, (err or b"")[-200:].decode(errors="replace"), text)
            return False
        except FileNotFoundError:
            log.warning("hermes CLI not found — notification dropped to log: %s", text)
            return False
        except Exception:  # noqa: BLE001 — never let a notification break the caller
            log.exception("hermes notify failed")
            return False


def get_notifier() -> Notifier:
    kind = _env("CURLYOS_NOTIFIER", "null").strip().lower()
    if kind in ("", "null", "log"):
        return NullNotifier()
    if kind == "hermes":
        return HermesNotifier()
    log.warning("unknown CURLYOS_NOTIFIER=%r — using NullNotifier", kind)
    return NullNotifier()
