"""CAI Application: Impala MCP server  (Application script: scripts/cai/app_mcp_server.py)

Serves MCP at https://<this-app-subdomain>.<CDSW_DOMAIN>/mcp.

Env vars (Application settings):
  MCP_ALLOWED_HOSTS  optional, comma-separated Host values to accept, e.g.
                     "zero-query-mcp.<your CDSW_DOMAIN>". If unset, the Host check is
                     disabled; the server only listens on 127.0.0.1 behind the CAI proxy.
  ZQ_MAX_ROWS        optional row cap (default 200)
  ZQ_KEYTAB          optional keytab path relative to the project root; with ZQ_KRB_PRINCIPAL the
                     launcher runs `kinit -kt` at start and refreshes hourly (needed for Impala)
  ZQ_KRB_PRINCIPAL   principal in the keytab, e.g. svc-account@EXAMPLE.COM
"""

import os
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
except NameError:
    sys.path.insert(0, str(Path.cwd() / "scripts" / "cai"))

from _common import app_port, run_component, start_kerberos  # noqa: E402

run_component(
    "zq-mcp",
    {
        "MCP_IMPALA_HOST": "127.0.0.1",
        "MCP_IMPALA_PORT": app_port(),
        "MCP_HOST_CHECK": os.environ.get("MCP_HOST_CHECK")
        or ("true" if os.environ.get("MCP_ALLOWED_HOSTS") else "false"),
        **start_kerberos(),
    },
)
