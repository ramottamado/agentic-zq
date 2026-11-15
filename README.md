# Zero Query

Ask questions about data in a Cloudera **Impala Virtual Warehouse** in plain language, with no SQL. Zero Query turns a question into a read-only query, runs it against the warehouse, and answers in words with the SQL and the result table alongside.

It runs on **Cloudera AI (CAI) Workbench** as three Applications. Agents are built with **LangGraph**; the LLM is served through **OpenRouter**. Out of the box it runs entirely offline against a mock warehouse and a rule-based mock LLM, so you can try it without credentials.

## How it works

```
UI  ->  Backend (FastAPI)  ->  LangGraph agents  ->  MCP client  ->  MCP server "impala"  ->  Warehouse
```

1. The **router** sends each question to the **general agent** (small talk, "what can you do?") or the **SQL agent**.
2. The SQL agent loads the schema, generates SQL, executes it, and summarizes the result. If execution fails, it loops back with the error message, up to `ZQ_MAX_SQL_ATTEMPTS` times.
3. All data access goes through the **MCP server**, which is where the guardrails live: a single read-only `SELECT`/`WITH` statement, a row cap, and a database allowlist. The agents cannot bypass them.

The backend contract the UI depends on is `POST /api/ask` returning `answer, route, sql, columns, rows, truncated, error`.

## Quick start (mock mode)

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                          # install dependencies
uv run pytest                    # tests: no network, no LLM, no open ports needed
python scripts/start_app.py      # MCP server + backend + UI on $CDSW_APP_PORT (default 8090)
```

Open the app and ask, for example, "revenue by region". With no `OPENROUTER_API_KEY` the mock LLM answers a fixed set of sample questions over the seeded `sales` database (`customers`, `products`, `orders`).

To use a real LLM, set `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` (an OpenRouter model slug).

## Connecting to a real Impala warehouse

Set `ZQ_WAREHOUSE=impala` on the MCP server, plus:

| Variable | Purpose |
|---|---|
| `IMPALA_HOST`, `IMPALA_PORT` (443), `IMPALA_HTTP_PATH` (`cliservice`) | Coordinator endpoint (HTTP transport over TLS) |
| `IMPALA_KRB_SERVICE_NAME` (`hive`), `IMPALA_KRB_HOST` | Kerberos (GSSAPI) service name and host |
| `IMPALA_DATABASES` (`default`) | Databases the app exposes. This is in addition to, not instead of, your Ranger policies |
| `IMPALA_SSL_VERIFY` (`false`), `IMPALA_CA_CERT` | TLS verification is **off by default**. Provide a CA certificate path and set `IMPALA_SSL_VERIFY=true` to turn it on |
| `ZQ_KEYTAB`, `ZQ_KRB_PRINCIPAL` | Optional. The MCP launcher runs `kinit -kt` at start and refreshes hourly |

Authentication uses the process's Kerberos ticket cache. Without a keytab, you must renew the ticket yourself; CAI-issued tickets last about 15 minutes.

Generated SQL runs on Impala, not SQLite. The mock warehouse's dialect differs (date functions especially), so the mock sample questions are not a guide to what works against Impala.

## Deploying on Cloudera AI

Step-by-step setup, changing environment variables, and troubleshooting are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). `scripts/cai_apps.py` creates, configures and restarts the apps through the CAI API.

Three Applications, one launcher script each in `scripts/cai/`. Each launcher syncs the shared `.venv` with `uv sync --inexact`, then runs its component on `127.0.0.1:$CDSW_APP_PORT`.

| Application | Script | Health path (`CDSW_APP_POLLING_ENDPOINT`) |
|---|---|---|
| MCP server | `scripts/cai/app_mcp_server.py` | `/health` |
| Backend API | `scripts/cai/app_backend.py` | `/api/health` |
| Frontend | `scripts/cai/app_frontend.py` | `/health` |

- **Only the frontend is for users.** It serves the UI and proxies `/api/*` to the backend server-side, so the browser needs no CORS and never sees the token.
- **App-to-app calls** go through the CAI proxy at `https://<subdomain>.$CDSW_DOMAIN` with `Authorization: Bearer $ZQ_SERVICE_TOKEN`. Point apps at each other with `ZQ_MCP_SUBDOMAIN` / `ZQ_BACKEND_SUBDOMAIN` (or the full `MCP_IMPALA_URL` / `ZQ_BACKEND_URL`).
- **`ZQ_SERVICE_TOKEN`** must be a long-lived key you set deliberately on all three apps. Don't use the `CDSW_API_KEY` that CAI injects into apps; it belongs to whoever runs the app and may be rotated.
- **Run the apps as a service account** (a CAI machine user added to the project) instead of a person, so the Kerberos identity and API keys don't depend on one user.
- The MCP SDK rejects proxied requests with `421 Invalid Host header` unless its host check is off. `app_mcp_server.py` disables it unless `MCP_ALLOWED_HOSTS` is set. Leave that alone.

## Configuration

Everything is read from environment variables; set secrets as CAI project or application variables. The full list, with defaults, is in [CLAUDE.md](CLAUDE.md#configuration-env-vars). The ones you will touch most:

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | unset | Enable the real LLM (model required with the key) |
| `OPENROUTER_APP_URL` / `OPENROUTER_APP_TITLE` / `OPENROUTER_APP_HIDDEN` | unset / `zq` / `true` | OpenRouter app attribution. The URL is what makes OpenRouter create the app entry; `hidden` keeps it out of public rankings, but only when the entry is first created |
| `ZQ_WAREHOUSE` | `mock` | `mock` or `impala` |
| `ZQ_MAX_ROWS` / `ZQ_MAX_SQL_ATTEMPTS` | 200 / 3 | Result row cap / SQL repair attempts |
| `ZQ_SERVICE_TOKEN` | unset | Bearer token on frontend to backend to MCP calls |

## Project layout

```
src/zero_query/
  agents/        LangGraph graph: router, general agent, SQL agent subgraph, MCP client, LLM factory
  backend/       FastAPI app (POST /api/ask, GET /api/health)
  frontend/      Server for the separate UI app (static files + /api proxy)
  mcp_impala/    MCP server (tools + guardrails) and Warehouse implementations
  config.py      Settings from environment variables
frontend/        Static UI (HTML/JS/CSS)
scripts/         start_app.py (all-in-one), cai/ launchers, cai_apps.py (manage the CAI apps)
deploy/          apps.json: non-secret definition of the three CAI Applications
docs/            DEPLOYMENT.md: deploying and operating the apps
tests/           pytest suite
```

## Development

```bash
uv run ruff check src tests scripts && uv run ruff format src tests scripts
uv run pytest tests/test_graph.py::test_mock_sql_route     # a single test
```

- Only lint paths under `src/`, `tests/` and `scripts/`. On CAI the project root is also `$HOME`, so `.venv`, `.cache` and other dotfolders live there and must never be touched by a formatter.
- Every agent node must keep working when no LLM is configured (the mock path), which is what lets the test suite run offline.
- To add a specialist agent: add it to `Route`, teach `router.py`, and add a node and edge in `graph.py`.
- The `mcp` SDK here is v2. `FastMCP` is now `mcp.server.mcpserver.MCPServer`, and `Client(server)` connects in-process, which is how tests avoid ports.

## Security notes

- **Guardrails are in the MCP server, and Ranger is the real boundary.** The read-only check and database allowlist are a second layer, not a replacement for warehouse permissions. The `otherdb.table` check is a heuristic.
- **TLS verification to Impala is off by default.** That exposes the connection to impersonation. Supply a CA certificate and enable it.
- **Keytabs are credentials.** They belong in the gitignored `keytabs/` folder, never in git, and should not be world-readable.
- LLM output is untrusted: the model only ever reaches the warehouse through the guarded MCP tools.
