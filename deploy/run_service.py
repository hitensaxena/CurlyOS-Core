#!/usr/bin/env python3
"""LaunchAgent entrypoint: private environment, stable paths, data-store readiness."""
from pathlib import Path
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
load_dotenv(ROOT / ".env", override=True)
if sys.argv[1] == "api":
    subprocess.run(["/usr/bin/open", "-gja", "OrbStack"], check=False)
    ports = [54321, 6379]
    gateway = urlparse(os.environ.get("CURLYOS_LLM_BASE_URL", ""))
    if gateway.hostname in ("127.0.0.1", "localhost") and gateway.port == 20128:
        subprocess.run(["/usr/bin/open", "-gja", "OmniRoute"], check=False)
        ports.append(20128)
    for attempt in range(90):
        try:
            for port in ports:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass
            break
        except OSError:
            time.sleep(1)
    args = [sys.executable, "-m", "uvicorn", "api_server:app", "--host", "127.0.0.1", "--port", "8643"]
elif sys.argv[1] == "mcp":
    args = [sys.executable, "-m", "mcp_server.server"]
else:
    raise SystemExit("Expected api or mcp")
os.execv(args[0], args)
