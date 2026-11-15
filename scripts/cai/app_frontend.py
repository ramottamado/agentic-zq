"""CAI Application: web UI  (Application script: scripts/cai/app_frontend.py)

Business users open this Application. It serves the UI and forwards /api/* to the
backend Application server-side.

Env vars (Application settings):
  ZQ_BACKEND_SUBDOMAIN  subdomain of the backend Application (or ZQ_BACKEND_URL = full https URL)
  ZQ_SERVICE_TOKEN      token sent as "Authorization: Bearer ..." to the backend Application
  ZQ_BACKEND_TIMEOUT    optional, seconds to wait for an answer (default 180)
"""

import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
except NameError:
    sys.path.insert(0, str(Path.cwd() / "scripts" / "cai"))

from _common import app_port, require_env, run_component  # noqa: E402

app_port()
require_env("ZQ_BACKEND_URL", "ZQ_BACKEND_SUBDOMAIN")
run_component("zq-frontend")
