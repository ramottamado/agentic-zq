# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: Zero Query

Zero Query lets business users ask questions about data in a Cloudera **Impala Virtual Warehouse** in natural language, without writing SQL. It runs on **Cloudera AI (CAI) Workbench**, Python 3.12, managed with **uv**. Agents are built with **LangGraph**; the LLM is served through **OpenRouter**.

Current state: the three CAI Applications run against the **real Impala VW** (`ZQ_WAREHOUSE=impala`, databases set by `IMPALA_DATABASES`) with a real LLM through OpenRouter. The mock warehouse (in-memory SQLite) and mock LLM are the *code defaults*, kept so tests and local development run offline with no credentials; they are not what is deployed. The mock SQL paths target the sample `sales` schema, not the real warehouse's tables.

## Commands

uv is installed at `~/.local/bin/uv` (add it to `PATH` if `uv` is not found).

```bash
uv sync                                   # install deps into .venv (from uv.lock)
uv run pytest                             # all tests (no network, no LLM, no ports needed)
uv run pytest tests/test_graph.py::test_mock_sql_route   # single test
uv run ruff check . && uv run ruff format .               # lint / format

python scripts/start_app.py               # all-in-one: MCP server (bg) + backend serving UI on $CDSW_APP_PORT
uv run zq-mcp                             # MCP server only (http://127.0.0.1:8765/mcp)
uv run zq-backend                         # backend (+ UI unless ZQ_SERVE_UI=false); needs zq-mcp
uv run zq-frontend                        # UI + /api proxy; needs ZQ_BACKEND_URL

python scripts/cai_apps.py list           # manage the deployed CAI Applications (env, set, restart, ...); see docs/DEPLOYMENT.md
```

## Deployment (Cloudera AI Applications)

Three Applications, one script each in `scripts/cai/` (run with the runtime's system Python; they `uv sync --inexact` the shared `.venv`, then run the component on `127.0.0.1:$CDSW_APP_PORT`):

| Application | Script | Key env vars |
|---|---|---|
| MCP server (`zq-mcp`) | `scripts/cai/app_mcp_server.py` | `ZQ_WAREHOUSE`, `IMPALA_*`, `ZQ_KEYTAB` + `ZQ_KRB_PRINCIPAL`, `MCP_ALLOWED_HOSTS` (optional) |
| Backend API (`zq-backend`) | `scripts/cai/app_backend.py` | `ZQ_MCP_SUBDOMAIN` or `MCP_IMPALA_URL`, `ZQ_SERVICE_TOKEN`, `OPENROUTER_*` |
| Frontend (`zq`) | `scripts/cai/app_frontend.py` | `ZQ_BACKEND_SUBDOMAIN` or `ZQ_BACKEND_URL`, `ZQ_SERVICE_TOKEN` |

- App-to-app calls go through the CAI proxy at `https://<subdomain>.$CDSW_DOMAIN` (derived from `*_SUBDOMAIN` vars) with `Authorization: Bearer $ZQ_SERVICE_TOKEN`. Business users open only the Frontend app; it proxies `/api/*` server-side, so the browser never needs CORS or the token.
- The MCP SDK auto-enables a Host-header check for 127.0.0.1 binds that rejects proxied requests (`421 Invalid Host header`). `app_mcp_server.py` disables it unless `MCP_ALLOWED_HOSTS` is set; don't remove that.
- **Run-as and tokens:** the apps run as a dedicated machine user (service account), which must first be added to the project as a run-as account. `ZQ_SERVICE_TOKEN` is a dedicated key set explicitly on all three apps. Do not fall back to the injected `CDSW_API_KEY`/`CDSW_APIV2_KEY`: they belong to whoever runs the app and may be rotated.
- **Health checks:** set `CDSW_APP_POLLING_ENDPOINT` per app: MCP `/health`, backend `/api/health`, frontend `/health`. The MCP and frontend `/health` are liveness only (they never touch the warehouse or backend), so a Kerberos or warehouse problem cannot make CAI restart-loop the app.
- **Kerberos:** CAI starts each app with a ticket for the run-as user, but it lasts ~15 min. With `ZQ_KEYTAB` (path relative to the project root, e.g. `keytabs/svc-account.keytab`) and `ZQ_KRB_PRINCIPAL` set, `app_mcp_server.py` runs `kinit -kt` at start and every `ZQ_KRB_REFRESH_SECONDS` into its own cache (`ZQ_KRB5_CCACHE`, default `/tmp/zq_krb5cc_<uid>`), so it never hits the renewal limit. A failed login is fatal only when `ZQ_WAREHOUSE=impala`. Keytabs live in the gitignored `keytabs/`; never commit them.
- Manage the apps with `python scripts/cai_apps.py` (list / env / set / restart / stop / run-as / create from `deploy/apps.json`); full guide in `docs/DEPLOYMENT.md`. Facts to remember: creating an app starts it immediately; `PATCH` takes `environment` as a JSON-encoded string and replaces it whole (always send the full dict); env var changes need a restart. Never print secrets: `cai_apps.py env` masks them, and `set KEY=@file` reads values from a file.
- Launchers must never `uv sync` without `--inexact`: all apps and sessions share one `.venv`, and an exact `--no-dev` sync would strip dev tools.

**Never run ruff (or any formatter/linter) with an explicit path outside `src/ tests/ scripts/`.** The project root is also `$HOME`, so `.cache/uv`, `.venv`, `.claude`, `.local` live here. `pyproject.toml` restricts ruff via `include`/`extend-exclude`; keep those settings. If cache or venv files are ever modified, rebuild with `uv cache clean && rm -rf .venv && uv sync` (uv hardlinks the venv from its cache, so editing one edits both).

## Architecture

```
UI (frontend/) -> Backend (FastAPI) -> LangGraph agents -> MCP client -> MCP server "impala" -> Warehouse
```

- **frontend/**: static HTML/JS, served either by the backend at `/` (all-in-one) or by `src/zero_query/frontend/server.py` (separate app that proxies `/api/*` to the backend). Talks only to `POST api/ask` and `GET api/health` using *relative* URLs (CAI serves apps under a path prefix).
- **backend** (`src/zero_query/backend/app.py`): `create_app(graph=None)` builds the graph at startup; tests inject a graph. The `AskResponse` contract (`answer, route, sql, columns, rows, truncated, error`) is what the UI depends on; agents can change freely behind it.
- **agents** (`src/zero_query/agents/`): `graph.py` is `router -> {general_agent | sql_agent}`. `sql_agent` is its own compiled subgraph: `load_schema -> generate_sql -> execute_sql -> summarize`, looping back to `generate_sql` with the error message on failure up to `ZQ_MAX_SQL_ATTEMPTS`. All agents share `ZeroQueryState` (`state.py`). To add a specialist (e.g. a semantic-layer agent over a Cube/dbt MCP server): add to `Route`, teach `router.py`, add a node/edge in `graph.py`.
- **Mock LLM mode**: when `OPENROUTER_API_KEY` is unset, `get_chat_model()` returns `None` and every agent uses a rule-based `mock_*` path. Every agent node must keep handling `llm is None`. OpenRouter is used via `ChatOpenAI` with OpenRouter's base URL; `OPENROUTER_MODEL` is required when the key is set.
- **MCP server** (`src/zero_query/mcp_impala/server.py`): tools `list_tables`, `describe_table`, `execute_query`. **Guardrails live here, not in agents**: read-only single SELECT/WITH statement, row cap (`ZQ_MAX_ROWS`). Raise `ToolError` for anticipated failures; other exceptions are masked to a generic message the agent cannot learn from.
- **Warehouse** (`warehouse.py`): `Warehouse` protocol is the seam between the MCP tools and the engine. `ImpalaWarehouse` (impyla, GSSAPI) authenticates from the process's Kerberos cache (`KRB5CCNAME`; the MCP launcher keeps it fresh from a keytab, see Deployment), opens a connection per call, and rejects `otherdb.table` references outside `IMPALA_DATABASES` (heuristic; Ranger is the real boundary). `MockWarehouse` is seeded, deterministic SQLite with schema `sales` (`customers`, `products`, `orders`) and `PRAGMA query_only`. SQLite's dialect is not Impala's, so SQL that works against the mock may fail on Impala (date functions especially).

### MCP SDK is v2
`mcp` 2.x differs from most examples online: `FastMCP` is now `mcp.server.mcpserver.MCPServer`; the client is `mcp.Client(url_or_server)`; results use `structured_content` / `is_error`. `Client(MCPServer_instance)` connects in-process, which is how tests run without ports (`tests/conftest.py`).

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | unset (mock LLM) | Enables the real LLM |
| `OPENROUTER_MODEL` | none | OpenRouter model slug, required with the key |
| `OPENROUTER_APP_URL` / `OPENROUTER_APP_TITLE` / `OPENROUTER_APP_HIDDEN` | unset / zq / true | OpenRouter app attribution: sent as `HTTP-Referer`, `X-OpenRouter-Title`, `X-OpenRouter-App-Visibility: hidden`. The URL is required for OpenRouter to create the (hidden) app entry; visibility only applies when the app is first created |
| `CDSW_APP_PORT` | 8090 | Backend/frontend port (set by CAI). Bind host `ZQ_APP_HOST`=127.0.0.1 |
| `MCP_IMPALA_HOST` / `MCP_IMPALA_PORT` / `MCP_IMPALA_URL` | 127.0.0.1 / 8765 / derived | MCP server location |
| `ZQ_MCP_SUBDOMAIN` / `ZQ_BACKEND_SUBDOMAIN` | unset | Derive `https://<sub>.$CDSW_DOMAIN` URLs for app-to-app calls |
| `ZQ_BACKEND_URL` | derived | Where the frontend forwards `/api/*` |
| `ZQ_SERVICE_TOKEN` | unset | Bearer token on frontend→backend→MCP calls |
| `ZQ_SERVE_UI` | true (false in `app_backend.py`) | Backend also serves `frontend/` |
| `MCP_HOST_CHECK` / `MCP_ALLOWED_HOSTS` | true / empty | MCP Host-header validation (see Deployment) |
| `ZQ_MAX_ROWS` / `ZQ_MAX_SQL_ATTEMPTS` | 200 / 3 | Result row cap / SQL repair attempts |
| `ZQ_KEYTAB` / `ZQ_KRB_PRINCIPAL` | unset | MCP launcher: keytab path and principal for `kinit -kt` (set both or neither) |
| `ZQ_KRB5_CCACHE` / `ZQ_KRB_REFRESH_SECONDS` | /tmp/zq_krb5cc_<uid> / 3600 | Credential cache the launcher writes / ticket refresh interval |
| `CDSW_APP_POLLING_ENDPOINT` | CAI default | Health path CAI polls (see Deployment); set on each app |
| `ZQ_WAREHOUSE` | mock | `mock` (SQLite) or `impala` (needs `IMPALA_HOST`) |
| `IMPALA_HOST` / `IMPALA_PORT` / `IMPALA_HTTP_PATH` | none / 443 / cliservice | Impala coordinator (HTTP transport, TLS) |
| `IMPALA_KRB_SERVICE_NAME` / `IMPALA_KRB_HOST` | hive / unset | Kerberos service name and host for GSSAPI |
| `IMPALA_SSL_VERIFY` / `IMPALA_CA_CERT` | false / unset | TLS verification is **off** by default; set a CA cert path and `IMPALA_SSL_VERIFY=true` to enable |
| `IMPALA_DATABASES` | default | Comma-separated databases the app exposes (on top of Ranger) |

Set secrets as CAI project or application environment variables. `.gitignore` ignores all dotfiles (`.*`), including `.env` and `.claude/`.
