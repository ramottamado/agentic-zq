# Deploying and operating Zero Query on Cloudera AI

Zero Query runs as three Cloudera AI (CAI) Applications built from the same project. This guide covers creating them, inspecting and changing their environment variables, restarting, and the traps we hit along the way.

Everything below uses [`scripts/cai_apps.py`](../scripts/cai_apps.py), a standard-library script over the CAI API v2. Run it from a CAI session in this project (the session already has `CDSW_DOMAIN`, `CDSW_PROJECT_ID` and `CDSW_APIV2_KEY`):

```bash
python scripts/cai_apps.py --help
```

## The three applications

| Subdomain | Script | Role | Health path |
|---|---|---|---|
| `zq-mcp` | `scripts/cai/app_mcp_server.py` | MCP server: tools and guardrails over the warehouse | `/health` |
| `zq-backend` | `scripts/cai/app_backend.py` | FastAPI + LangGraph agents | `/api/health` |
| `zq` | `scripts/cai/app_frontend.py` | Web UI, proxies `/api/*` to the backend | `/health` |

`deploy/apps.json` is the non-secret definition of all three (names, scripts, resources, default env vars). Business users only ever open the frontend.

## Prerequisites

1. **A service account to run the apps.** Add a CAI machine user to the project as a run-as account, and note its numeric user ID. Without this, apps run as whoever started them, and their Kerberos identity and injected API keys change with that person.
2. **Read access for that account to the project directory**, including `.venv` and the `keytabs/` folder. The apps run the project's code from the shared filesystem.
3. **A Kerberos keytab** (for the real Impala warehouse). Put it in `keytabs/` (gitignored) and keep it unreadable by other users where your setup allows. Find the principal with `klist -kt keytabs/<file>.keytab`.
4. **A long-lived service token** for app-to-app calls: a CAI API key you create on purpose. The CAI proxy accepts it as a Bearer token. **Do not reuse the `CDSW_API_KEY` / `CDSW_APIV2_KEY` that CAI injects into apps**: they belong to whoever runs the app and may be rotated underneath you.
5. **An OpenRouter API key and model slug** if you want the real LLM. Without them the backend uses the rule-based mock LLM.

Put each secret in a file readable only by you (for example `chmod 600`), and pass it as `KEY=@/path/to/file`. That keeps it out of shell history and out of the conversation.

## First deployment

1. **Fill in the non-secret values** the manifest expects, as environment variables in your session:

   ```bash
   export IMPALA_HOST=<coordinator host>            # Impala virtual warehouse coordinator
   export IMPALA_KRB_HOST=<kerberos host>           # host part of the Impala service principal
   export IMPALA_DATABASES=<db1,db2>                # optional, default: default
   export ZQ_KEYTAB=keytabs/<file>.keytab           # relative to the project root
   export ZQ_KRB_PRINCIPAL=<principal>@<REALM>
   export OPENROUTER_MODEL=<vendor/model>
   export OPENROUTER_APP_URL=<url>                  # see "OpenRouter attribution" below
   ```

2. **Preview, then create.** `create` skips apps that already exist and tells you which variables are missing:

   ```bash
   python scripts/cai_apps.py create --dry-run
   python scripts/cai_apps.py create
   ```

   > CAI **starts an application the moment it is created**. `create` stops each app again straight away so nothing runs half-configured; pass `--start` to leave them running.

3. **Set the secrets** (values read from files):

   ```bash
   for app in zq-mcp zq-backend zq; do
     python scripts/cai_apps.py set $app ZQ_SERVICE_TOKEN=@$HOME/secrets/service-token
   done
   python scripts/cai_apps.py set zq-backend OPENROUTER_API_KEY=@$HOME/secrets/openrouter-key
   ```

4. **Run as the service account:**

   ```bash
   for app in zq-mcp zq-backend zq; do python scripts/cai_apps.py run-as $app <user-id>; done
   ```

   If CAI answers `500: Run as user does not have the right permissions for project`, the account has not been added to the project as a machine user yet. Add it in the CAI UI first (it is not a normal collaborator, and giving it write access is not the same thing).

5. **Start, in dependency order, and verify:**

   ```bash
   python scripts/cai_apps.py restart zq-mcp
   python scripts/cai_apps.py restart zq-backend
   python scripts/cai_apps.py restart zq
   python scripts/cai_apps.py list
   ```

   `restart` also starts a stopped app and waits until it is running.

## Environment variables per app

Names only; values live in the apps. `*` marks secrets (masked by the script).

| App | Variables |
|---|---|
| `zq-mcp` | `ZQ_WAREHOUSE`, `IMPALA_HOST`, `IMPALA_PORT`, `IMPALA_HTTP_PATH`, `IMPALA_KRB_SERVICE_NAME`, `IMPALA_KRB_HOST`, `IMPALA_SSL_VERIFY`, `IMPALA_CA_CERT`, `IMPALA_DATABASES`, `ZQ_MAX_ROWS`, `ZQ_KEYTAB`, `ZQ_KRB_PRINCIPAL`, `ZQ_SERVICE_TOKEN`\*, `CDSW_APP_POLLING_ENDPOINT` |
| `zq-backend` | `ZQ_MCP_SUBDOMAIN`, `ZQ_MAX_SQL_ATTEMPTS`, `OPENROUTER_API_KEY`\*, `OPENROUTER_MODEL`, `OPENROUTER_APP_URL`, `ZQ_SERVICE_TOKEN`\*, `CDSW_APP_POLLING_ENDPOINT` |
| `zq` | `ZQ_BACKEND_SUBDOMAIN`, `ZQ_SERVICE_TOKEN`\*, `CDSW_APP_POLLING_ENDPOINT` |

The full list with defaults is in [CLAUDE.md](../CLAUDE.md#configuration-env-vars).

## Inspecting and changing variables

```bash
python scripts/cai_apps.py env zq-backend                 # secrets shown as <set, N chars>
python scripts/cai_apps.py env zq-backend --reveal        # prints secret values: mind your terminal and logs
python scripts/cai_apps.py set zq-backend OPENROUTER_MODEL=vendor/model --restart
python scripts/cai_apps.py set zq-mcp --unset IMPALA_CA_CERT
python scripts/cai_apps.py set zq-backend OPENROUTER_MODEL=x --dry-run     # show the change, do nothing
```

- **Changes need a restart.** Environment variables are read at process start. `--restart` does it for you.
- `set` merges into the existing environment. (Under the hood, the API takes `environment` as a JSON-encoded **string** and replaces it whole, so a naive PATCH with one key would wipe the rest. The script always sends the full set.)
- App-level variables are visible to everyone who can see the app's settings. Treat them as such when deciding what goes there.

## Common operations

| Task | How |
|---|---|
| Deploy a code change | Apps run the project's files in place. Commit/save, then `restart` the affected app. The launcher runs `uv sync --inexact` on every start, so new dependencies install then. |
| Switch to the mock warehouse | `set zq-mcp ZQ_WAREHOUSE=mock --restart`. The mock serves sample `sales` data only. |
| Change the LLM | `set zq-backend OPENROUTER_MODEL=<slug> --restart` |
| Rotate the service token | `set` the new `ZQ_SERVICE_TOKEN=@file` on **all three** apps, then restart all three. The old token stops working as soon as you revoke it, so do the swap quickly. |
| Turn on TLS verification to Impala | Put the CA certificate in the project, then `set zq-mcp IMPALA_CA_CERT=<path> IMPALA_SSL_VERIFY=true --restart`. |
| Change a subdomain | `PATCH` the app's `subdomain` (the API supports it) and restart. **The old URL stops working** (certificate mismatch), so anyone with a bookmark needs the new one. Update `ZQ_MCP_SUBDOMAIN` / `ZQ_BACKEND_SUBDOMAIN` on the apps that point at it. |
| Stop everything | `stop zq`, `stop zq-backend`, `stop zq-mcp` |

### OpenRouter attribution

The backend identifies itself to OpenRouter with the title `zq`, and asks to be hidden from public rankings. OpenRouter only creates an app entry when `OPENROUTER_APP_URL` (sent as `HTTP-Referer`) is set, and the "hidden" choice only counts when the entry is first created: it cannot be changed afterwards except by OpenRouter support. Choose the URL deliberately before the first request. It does not have to be a real site, but it is how OpenRouter tells apps apart, so use one nobody else would.

## Verifying a deployment

The CAI proxy requires authentication. Browsers log in; scripts send the service token:

```bash
TOKEN=$(cat ~/secrets/service-token)
curl -s -H "Authorization: Bearer $TOKEN" https://zq-backend.$CDSW_DOMAIN/api/health
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"question": "how many rows are in each table?"}' https://zq-backend.$CDSW_DOMAIN/api/ask
```

A request without the token is answered with a `302` redirect to the CAI login page. That means the app is up and protected, not broken.

`/api/health` reports `llm` (`openrouter` or `mock`), the model, and the MCP URL. `/health` on the MCP server and frontend is liveness only: it deliberately does not touch the warehouse or backend, so a Kerberos or warehouse problem cannot make CAI restart-loop the app.

Application logs are in the CAI UI (the application's page).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `421 Invalid Host header` from the MCP app | The MCP SDK's host check rejects proxied requests. The MCP launcher turns it off unless `MCP_ALLOWED_HOSTS` is set; don't remove that. |
| MCP app exits at start with "Kerberos login failed" | Only when `ZQ_WAREHOUSE=impala`. The run-as account can't read the keytab (check `/home/cdsw` and `keytabs/` permissions), the principal doesn't match the keytab, or the realm/KDC is unreachable. |
| Queries fail with a Kerberos or authentication error after hours | The ticket expired. With `ZQ_KEYTAB` and `ZQ_KRB_PRINCIPAL` set, the MCP launcher refreshes it hourly; check both are set and the logs show "refreshed Kerberos ticket". CAI-issued tickets alone last only about 15 minutes. |
| `/api/ask` returns an answer saying it couldn't build a query, naming `sales.*` | The backend is on the mock LLM (no `OPENROUTER_API_KEY`), which only knows the sample schema. |
| `502 ... Error code: 429` from `/api/ask` | OpenRouter rate-limited the model. Free models do this often; retry later or change model. |
| One `502 ... 'NoneType' object has no attribute 'getpeername'` right after a restart | Seen once on the first request after restarting the backend; it did not recur on retry. |
| Answer is wrong but there is no error | The model guessed a column value (for example `'CREDIT'` instead of `'C'`) and got an empty result. A larger model, or putting sampled column values in the schema prompt, helps. |
| `set` says the app is fine but nothing changed | You didn't restart. Variables only apply on start. |
| Setting run-as fails with `500 ... right permissions` | The account isn't a project machine user yet; see step 4 under "First deployment". |
| Old app URL shows a certificate error | The subdomain was changed. Use the new URL. |

## Without the script

The script is a thin wrapper over `https://$CDSW_DOMAIN/api/v2/projects/$CDSW_PROJECT_ID/applications` with `Authorization: Bearer $CDSW_APIV2_KEY`: `GET` lists, `POST` creates, `PATCH .../{id}` updates (`environment` as a JSON string, `run_as`, `subdomain`), and `POST .../{id}:restart` / `:stop` control state. The OpenAPI document is at `https://$CDSW_DOMAIN/api/v2/swagger.json`.
