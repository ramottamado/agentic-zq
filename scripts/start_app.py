"""All-in-one launcher: MCP server + backend serving the UI, in one process tree.

For local development, or a single CAI Application. For the three-Application
deployment use scripts/cai/app_*.py instead.

Starts the mock Impala MCP server in the background, waits until it accepts
connections, then runs the backend (which also serves the UI) on
127.0.0.1:$CDSW_APP_PORT in the foreground. Both run inside the project's uv venv.

Also works locally:  python scripts/start_app.py
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def find_uv() -> str:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv).exists():
        sys.exit("uv not found. Install it with: pip install --user uv")
    return uv


def wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    raise TimeoutError(f"MCP server did not start on {host}:{port}")


def main() -> None:
    uv = find_uv()
    # --inexact: keep dev tools in the shared venv.
    subprocess.run([uv, "sync", "--frozen", "--no-dev", "--inexact"], cwd=ROOT, check=True)

    host = os.environ.get("MCP_IMPALA_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_IMPALA_PORT", "8765"))
    mcp = subprocess.Popen([uv, "run", "--no-sync", "zq-mcp"], cwd=ROOT)
    try:
        wait_for_port(host, port)
        backend = subprocess.Popen([uv, "run", "--no-sync", "zq-backend"], cwd=ROOT)
        signal.signal(signal.SIGTERM, lambda *_: backend.terminate())
        sys.exit(backend.wait())
    finally:
        mcp.terminate()
        mcp.wait(timeout=10)


if __name__ == "__main__":
    main()
