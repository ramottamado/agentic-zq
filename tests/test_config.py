from zero_query.config import get_settings
from zero_query.mcp_impala.server import transport_security


def test_app_urls_derived_from_subdomains(monkeypatch):
    monkeypatch.delenv("MCP_IMPALA_URL", raising=False)
    monkeypatch.delenv("ZQ_BACKEND_URL", raising=False)
    monkeypatch.setenv("CDSW_DOMAIN", "cai.example.com")
    monkeypatch.setenv("ZQ_MCP_SUBDOMAIN", "zq-mcp")
    monkeypatch.setenv("ZQ_BACKEND_SUBDOMAIN", "zq-api")
    s = get_settings()
    assert s.mcp_url == "https://zq-mcp.cai.example.com/mcp"
    assert s.backend_url == "https://zq-api.cai.example.com"


def test_explicit_url_wins(monkeypatch):
    monkeypatch.setenv("MCP_IMPALA_URL", "http://x:1/mcp/")
    monkeypatch.setenv("ZQ_MCP_SUBDOMAIN", "ignored")
    assert get_settings().mcp_url == "http://x:1/mcp"


def test_local_default_mcp_url(monkeypatch):
    for var in ("MCP_IMPALA_URL", "ZQ_MCP_SUBDOMAIN"):
        monkeypatch.delenv(var, raising=False)
    assert get_settings().mcp_url == "http://127.0.0.1:8765/mcp"


def test_mcp_transport_security(monkeypatch):
    monkeypatch.setenv("MCP_HOST_CHECK", "false")
    assert transport_security(get_settings()).enable_dns_rebinding_protection is False

    monkeypatch.setenv("MCP_HOST_CHECK", "true")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "zq-mcp.cai.example.com")
    ts = transport_security(get_settings())
    assert ts.enable_dns_rebinding_protection
    assert "zq-mcp.cai.example.com" in ts.allowed_hosts
    assert "https://zq-mcp.cai.example.com" in ts.allowed_origins


def test_openrouter_attribution_headers(monkeypatch):
    from zero_query.config import get_settings

    for var in ("OPENROUTER_APP_URL", "OPENROUTER_APP_TITLE", "OPENROUTER_APP_HIDDEN"):
        monkeypatch.delenv(var, raising=False)
    # Default: titled "zq" and hidden; no referer unless configured.
    assert get_settings().openrouter_headers == {
        "X-OpenRouter-Title": "zq",
        "X-OpenRouter-App-Visibility": "hidden",
    }
    monkeypatch.setenv("OPENROUTER_APP_URL", "https://zq.example.com")
    monkeypatch.setenv("OPENROUTER_APP_HIDDEN", "false")
    assert get_settings().openrouter_headers == {
        "X-OpenRouter-Title": "zq",
        "HTTP-Referer": "https://zq.example.com",
    }


def test_chat_model_sends_attribution_headers(monkeypatch):
    from zero_query.agents.llm import get_chat_model
    from zero_query.config import get_settings

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model")
    monkeypatch.setenv("OPENROUTER_APP_URL", "https://zq.example.com")
    model = get_chat_model(get_settings())
    assert model.default_headers["X-OpenRouter-App-Visibility"] == "hidden"
    assert model.default_headers["HTTP-Referer"] == "https://zq.example.com"
