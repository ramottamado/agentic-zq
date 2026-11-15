"""Shared launcher logic for the Cloudera AI Application scripts in this folder.

Each CAI Application runs in its own pod with the project filesystem mounted at
/home/cdsw, so all apps share the project's uv-managed .venv. Launchers run with the
runtime's system Python (no project deps), sync the venv, then run one component
via its console script in the foreground on 127.0.0.1:$CDSW_APP_PORT.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def project_root() -> Path:
    if root := os.environ.get("ZQ_PROJECT_ROOT"):
        return Path(root)
    try:
        return Path(__file__).resolve().parents[2]
    except NameError:  # some runners exec scripts without __file__
        return Path.cwd()


def find_uv() -> str:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv).exists():
        sys.exit("uv not found. In a CAI session terminal run: pip install --user uv")
    return uv


def app_port() -> str:
    port = os.environ.get("CDSW_APP_PORT")
    if not port:
        sys.exit("CDSW_APP_PORT is not set: run this script as a Cloudera AI Application.")
    return port


def require_env(*names: str) -> None:
    """Exit unless at least one of `names` is set."""
    if not any(os.environ.get(n) for n in names):
        sys.exit(f"Set one of {', '.join(names)} in the Application's environment variables.")


def start_kerberos() -> dict[str, str]:
    """Keep a Kerberos ticket fresh from a keytab, for components that talk to a Kerberized service.

    Active only when ZQ_KEYTAB (path, relative to the project root) and ZQ_KRB_PRINCIPAL are set.
    Uses a dedicated credential cache (ZQ_KRB5_CCACHE) so it never clobbers the ticket CAI injects,
    runs `kinit -kt` now and then every ZQ_KRB_REFRESH_SECONDS (default 3600). Returns env vars
    for the child process (KRB5CCNAME). A keytab ticket can always be re-acquired, so there is
    no renewal limit. Startup failure is fatal only when the real Impala warehouse is selected.
    """
    keytab, principal = os.environ.get("ZQ_KEYTAB"), os.environ.get("ZQ_KRB_PRINCIPAL")
    if not keytab and not principal:
        return {}
    if not (keytab and principal):
        sys.exit("Set both ZQ_KEYTAB and ZQ_KRB_PRINCIPAL (or neither).")
    path = Path(keytab)
    path = path if path.is_absolute() else project_root() / path
    ccache = os.environ.get("ZQ_KRB5_CCACHE") or f"/tmp/zq_krb5cc_{os.getuid()}"
    env = {"KRB5CCNAME": f"FILE:{ccache}"}
    interval = int(os.environ.get("ZQ_KRB_REFRESH_SECONDS", "3600"))
    needed = os.environ.get("ZQ_WAREHOUSE", "mock").lower() == "impala"

    def kinit() -> bool:
        try:
            done = subprocess.run(
                ["kinit", "-kt", str(path), principal],
                env={**os.environ, **env},
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[zero-query] kinit failed: {exc}", flush=True)
            return False
        if done.returncode:
            print(f"[zero-query] kinit failed: {done.stderr.strip()}", flush=True)
        return done.returncode == 0

    if not kinit():
        if needed:
            sys.exit(f"Kerberos login failed for {principal} using {path}.")
        print("[zero-query] continuing without a Kerberos ticket (warehouse is not impala)", flush=True)

    def refresh() -> None:
        while True:
            time.sleep(interval)
            if kinit():
                print(f"[zero-query] refreshed Kerberos ticket for {principal}", flush=True)

    threading.Thread(target=refresh, name="krb-refresh", daemon=True).start()
    return env


def run_component(entry_point: str, env: dict[str, str] | None = None) -> None:
    root, uv = project_root(), find_uv()
    # --inexact: never remove packages (e.g. dev tools) from the venv shared with sessions/other apps.
    subprocess.run([uv, "sync", "--frozen", "--no-dev", "--inexact"], cwd=root, check=True)
    child_env = {**os.environ, "ZQ_APP_HOST": "127.0.0.1", **(env or {})}
    print(f"[zero-query] starting {entry_point} on 127.0.0.1:{os.environ.get('CDSW_APP_PORT')}", flush=True)
    proc = subprocess.Popen([uv, "run", "--frozen", "--no-sync", entry_point], cwd=root, env=child_env)
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: proc.terminate())
    except ValueError:  # not in main thread; the child still dies with the pod
        pass
    sys.exit(proc.wait())
