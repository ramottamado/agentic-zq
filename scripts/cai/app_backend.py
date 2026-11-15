"""CAI Application: backend API + agents  (Application script: scripts/cai/app_backend.py)

Serves POST /api/ask and GET /api/health at https://<this-app-subdomain>.<CDSW_DOMAIN>.

Env vars (Application settings):
  ZQ_MCP_SUBDOMAIN    subdomain of the MCP server Application (or MCP_IMPALA_URL = full .../mcp URL)
  ZQ_SERVICE_TOKEN    token sent as "Authorization: Bearer ..." to the MCP Application
  OPENROUTER_API_KEY  optional; unset = mock LLM
  OPENROUTER_MODEL    required when OPENROUTER_API_KEY is set
  ZQ_SERVE_UI         optional, default false here (UI is its own Application)
"""

import os
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
except NameError:
    sys.path.insert(0, str(Path.cwd() / "scripts" / "cai"))

from _common import app_port, require_env, run_component  # noqa: E402

app_port()
require_env("MCP_IMPALA_URL", "ZQ_MCP_SUBDOMAIN")
run_component("zq-backend", {"ZQ_SERVE_UI": os.environ.get("ZQ_SERVE_UI", "false")})
