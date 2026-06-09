#!/usr/bin/env python3
"""Start the CurlyOS API server as a background daemon.

Usage: python3 start_api_server.py [--stop] [--status]
"""
import os
import sys
import signal
import subprocess
import time

PIDFILE = "/tmp/curlyos-api.pid"
PORT = os.environ.get("CURLYOS_API_PORT", "8643")
DSN = os.environ.get("CURLYOS_DATABASE_URL", "")

def start():
    if os.path.exists(PIDFILE):
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)  # Check if process exists
            print(f"CurlyOS API server already running (PID {pid})")
            return
        except ProcessLookupError:
            os.unlink(PIDFILE)
    
    # Start the server
    env = os.environ.copy()
    env["CURLYOS_API_PORT"] = PORT
    if DSN:
        env["CURLYOS_DATABASE_URL"] = DSN
    
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api_server:app",
         "--host", "127.0.0.1", "--port", PORT, "--log-level", "warning"],
        stdout=open("/tmp/curlyos-api.log", "w"),
        stderr=subprocess.STDOUT,
        env=env,
        cwd=os.path.expanduser("~/curlyos-core"),
    )
    
    with open(PIDFILE, "w") as f:
        f.write(str(proc.pid))
    
    # Wait for it to be ready
    import urllib.request
    for _ in range(10):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2)
            print(f"CurlyOS API server started on port {PORT} (PID {proc.pid})")
            return
        except Exception:
            pass
    
    print(f"CurlyOS API server started (PID {proc.pid}) but not yet responding")

def stop():
    if not os.path.exists(PIDFILE):
        print("CurlyOS API server not running (no PID file)")
        return
    
    with open(PIDFILE) as f:
        pid = int(f.read().strip())
    
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"CurlyOS API server stopped (PID {pid})")
    except ProcessLookupError:
        print(f"Process {pid} not found")
    
    os.unlink(PIDFILE)

def status():
    if not os.path.exists(PIDFILE):
        print("CurlyOS API server: not running (no PID file)")
        return
    
    with open(PIDFILE) as f:
        pid = int(f.read().strip())
    
    try:
        os.kill(pid, 0)
        import urllib.request
        resp = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2)
        health = resp.read().decode()[:100]
        print(f"CurlyOS API server: running (PID {pid}), health: {health}")
    except ProcessLookupError:
        print(f"CurlyOS API server: stale PID file (PID {pid} not running)")
        os.unlink(PIDFILE)
    except Exception as e:
        print(f"CurlyOS API server: running (PID {pid}) but not responding: {e}")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "stop":
        stop()
    elif cmd == "status":
        status()
    else:
        start()
