"""Runtime settings, read from environment variables (CAI project/application env vars)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_SQL_ATTEMPTS = 3


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _app_url(explicit_var: str, subdomain_var: str, path: str = "") -> str | None:
    """Explicit URL wins; else derive a CAI Application URL from its subdomain + CDSW_DOMAIN."""
    if url := os.environ.get(explicit_var):
        return url.rstrip("/")
    subdomain, domain = os.environ.get(subdomain_var), os.environ.get("CDSW_DOMAIN")
    if subdomain and domain:
        return f"https://{subdomain}.{domain}{path}"
    return None


@dataclass(frozen=True)
class Settings:
    # LLM (OpenRouter). If no API key is set, agents run in mock mode (rule-based, no LLM calls).
    openrouter_api_key: str | None
    openrouter_model: str | None
    # OpenRouter app attribution headers. HTTP-Referer (the app URL) is what makes OpenRouter
    # create an app entry; title and visibility are inert without it.
    openrouter_app_url: str | None
    openrouter_app_title: str
    openrouter_app_hidden: bool
    # MCP server for Impala
    mcp_host: str
    mcp_port: int
    mcp_url_override: str | None
    # Host-header (DNS-rebinding) check on the MCP server. Must be off, or list the proxy's Host
    # values, when the server sits behind the CAI application proxy.
    mcp_host_check: bool
    mcp_allowed_hosts: list[str]
    # Backend / UI. CAI Applications must listen on 127.0.0.1:$CDSW_APP_PORT.
    app_host: str
    app_port: int
    serve_ui: bool
    # Static UI files, served by the backend (all-in-one) or by the frontend app
    frontend_dir: Path
    # Frontend app: where to forward /api/* requests
    backend_url: str | None
    # Bearer token sent on app-to-app calls (frontend -> backend -> MCP) through the CAI proxy
    service_token: str | None
    # Guardrails applied by the MCP server
    max_rows: int
    # How many times the SQL agent may regenerate SQL after an execution error
    max_sql_attempts: int
    # Warehouse behind the MCP server: "mock" (SQLite) or "impala"
    warehouse: str
    impala_host: str | None
    impala_port: int
    impala_http_path: str
    impala_krb_service_name: str
    impala_krb_host: str | None
    # TLS: verification is off by default (self-signed certs); set a CA cert to turn it on.
    impala_ssl_verify: bool
    impala_ca_cert: str | None
    # App-level database scope (in addition to Ranger). Only these databases are exposed.
    impala_databases: list[str]

    @property
    def mcp_url(self) -> str:
        return self.mcp_url_override or f"http://{self.mcp_host}:{self.mcp_port}/mcp"

    @property
    def use_mock_llm(self) -> bool:
        return not self.openrouter_api_key

    @property
    def openrouter_headers(self) -> dict[str, str]:
        """App attribution headers (https://openrouter.ai/docs/app-attribution)."""
        headers = {"X-OpenRouter-Title": self.openrouter_app_title}
        if self.openrouter_app_url:
            headers["HTTP-Referer"] = self.openrouter_app_url
        if self.openrouter_app_hidden:
            headers["X-OpenRouter-App-Visibility"] = "hidden"
        return headers

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.service_token}"} if self.service_token else {}


def get_settings() -> Settings:
    app_port = os.environ.get("CDSW_APP_PORT") or os.environ.get("ZQ_APP_PORT", "8090")
    return Settings(
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
        openrouter_model=os.environ.get("OPENROUTER_MODEL") or None,
        openrouter_app_url=os.environ.get("OPENROUTER_APP_URL") or None,
        openrouter_app_title=os.environ.get("OPENROUTER_APP_TITLE") or "zq",
        openrouter_app_hidden=_flag("OPENROUTER_APP_HIDDEN", True),
        mcp_host=os.environ.get("MCP_IMPALA_HOST", "127.0.0.1"),
        mcp_port=int(os.environ.get("MCP_IMPALA_PORT", "8765")),
        mcp_url_override=_app_url("MCP_IMPALA_URL", "ZQ_MCP_SUBDOMAIN", "/mcp"),
        mcp_host_check=_flag("MCP_HOST_CHECK", True),
        mcp_allowed_hosts=[h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()],
        app_host=os.environ.get("ZQ_APP_HOST", "127.0.0.1"),
        app_port=int(app_port),
        serve_ui=_flag("ZQ_SERVE_UI", True),
        frontend_dir=Path(os.environ.get("ZQ_FRONTEND_DIR") or Path(__file__).resolve().parents[2] / "frontend"),
        backend_url=_app_url("ZQ_BACKEND_URL", "ZQ_BACKEND_SUBDOMAIN"),
        service_token=os.environ.get("ZQ_SERVICE_TOKEN") or None,
        max_rows=int(os.environ.get("ZQ_MAX_ROWS", "200")),
        max_sql_attempts=int(os.environ.get("ZQ_MAX_SQL_ATTEMPTS", DEFAULT_MAX_SQL_ATTEMPTS)),
        warehouse=os.environ.get("ZQ_WAREHOUSE", "mock").strip().lower(),
        impala_host=os.environ.get("IMPALA_HOST") or None,
        impala_port=int(os.environ.get("IMPALA_PORT", "443")),
        impala_http_path=os.environ.get("IMPALA_HTTP_PATH", "cliservice"),
        impala_krb_service_name=os.environ.get("IMPALA_KRB_SERVICE_NAME", "hive"),
        impala_krb_host=os.environ.get("IMPALA_KRB_HOST") or None,
        impala_ssl_verify=_flag("IMPALA_SSL_VERIFY", False),
        impala_ca_cert=os.environ.get("IMPALA_CA_CERT") or None,
        impala_databases=[d.strip() for d in os.environ.get("IMPALA_DATABASES", "default").split(",") if d.strip()],
    )
