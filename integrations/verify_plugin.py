"""Verify installed plugin discovery using the real Codex app-server, without a model turn."""
from pathlib import Path
import json
import selectors
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
CODEX = Path.home() / ".local/bin/codex"
EXPECTED = {"sessionStart", "userPromptSubmit", "stop", "preCompact"}


def main():
    process = subprocess.Popen([str(CODEX), "app-server", "--stdio"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)

    def rpc(request_id, method, params):
        process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not selector.select(1):
                continue
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("Codex app-server exited")
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]
        raise TimeoutError(method)

    try:
        rpc(1, "initialize", {"clientInfo": {"name": "curlyos-install-verification", "version": "1.0"},
                              "capabilities": {"experimentalApi": True}})
        process.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        process.stdin.flush()
        data = rpc(2, "hooks/list", {"cwds": [str(Path.home())]})["data"][0]
        hooks = [hook for hook in data["hooks"] if hook.get("pluginId") == "curlyos-memory@curlyos-local"]
        assert {hook["eventName"] for hook in hooks} == EXPECTED, "Expected four CurlyOS hooks"
        assert not data["errors"], data["errors"]
        assert not any("curlyos" in warning for warning in data["warnings"]), data["warnings"]
        plugin = rpc(3, "plugin/read", {"marketplacePath": str(ROOT / ".agents/plugins/marketplace.json"),
                                       "pluginName": "curlyos-memory"})["plugin"]
        assert "curlyos" in plugin["mcpServers"], "CurlyOS MCP connection missing"
        print("Codex discovers all four CurlyOS hooks and the curlyos MCP connection.")
        print("Hook trust:", ", ".join(sorted({hook["trustStatus"] for hook in hooks})))
        print("No CurlyOS discovery warnings or errors.")
    finally:
        selector.close()
        process.terminate()
        process.wait(timeout=5)


if __name__ == "__main__":
    main()
