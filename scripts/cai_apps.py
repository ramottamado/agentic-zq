#!/usr/bin/env python3
"""Manage the Zero Query Cloudera AI Applications through the CAI API v2 (standard library only).

Needs CDSW_DOMAIN, CDSW_PROJECT_ID and CDSW_APIV2_KEY, which a CAI session already has.
Run with the system Python; it does not need the project venv.

  python scripts/cai_apps.py list
  python scripts/cai_apps.py env zq-mcp                        # secrets masked; --reveal to show
  python scripts/cai_apps.py set zq-backend OPENROUTER_MODEL=vendor/model --restart
  python scripts/cai_apps.py set zq-backend ZQ_SERVICE_TOKEN=@/path/to/file   # value read from a file
  python scripts/cai_apps.py set zq-mcp --unset ZQ_KEYTAB
  python scripts/cai_apps.py restart zq-backend                # also starts a stopped app
  python scripts/cai_apps.py stop zq-mcp
  python scripts/cai_apps.py run-as zq-mcp 20                  # numeric id of a project machine user
  python scripts/cai_apps.py create [--dry-run] [--start]      # from deploy/apps.json

An app is addressed by subdomain or name. Full guide: docs/DEPLOYMENT.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
MANIFEST = Path(__file__).resolve().parents[1] / "deploy" / "apps.json"
TERMINAL = {"APPLICATION_RUNNING", "APPLICATION_STOPPED", "APPLICATION_FAILED"}


def is_secret(name: str) -> bool:
    return name.upper().endswith(SECRET_SUFFIXES)


def mask(env: dict[str, str]) -> dict[str, str]:
    return {k: (f"<set, {len(v)} chars>" if is_secret(k) else v) for k, v in env.items()}


def parse_assignments(items: list[str]) -> dict[str, str]:
    """`KEY=value`, or `KEY=@path` to read the value from a file (keeps secrets out of shell history)."""
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            sys.exit(f"Expected KEY=VALUE or KEY=@file, got: {key!r}")
        if value.startswith("@"):
            value = Path(value[1:]).expanduser().read_text().strip()
        out[key] = value
    return out


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str, environ: dict[str, str], missing: set[str]) -> str:
    def repl(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        if environ.get(name):
            return environ[name]
        if default is not None:
            return default
        missing.add(name)
        return m.group(0)

    return _VAR.sub(repl, value)


def find_app(apps: list[dict], ident: str) -> dict:
    for a in apps:
        if ident.lower() in (a["subdomain"].lower(), a["name"].lower(), a["id"]):
            return a
    sys.exit(f"No application {ident!r}. Known: {', '.join(a['subdomain'] for a in apps) or 'none'}")


def env_of(app: dict) -> dict[str, str]:
    return json.loads(app.get("environment") or "{}")


class Api:
    def __init__(self) -> None:
        try:
            self.domain, self.project = os.environ["CDSW_DOMAIN"], os.environ["CDSW_PROJECT_ID"]
            self.key = os.environ["CDSW_APIV2_KEY"]
        except KeyError as exc:
            sys.exit(f"{exc.args[0]} is not set. Run this from a CAI session in the project.")
        self.base = f"https://{self.domain}/api/v2"

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{self.base}{path}",
            json.dumps(body).encode() if body is not None else None,
            {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            sys.exit(f"{method} {path} failed: HTTP {exc.code} {exc.read().decode()[:300]}")

    @property
    def apps_path(self) -> str:
        return f"/projects/{self.project}/applications"

    def apps(self) -> list[dict]:
        return self.call("GET", self.apps_path).get("applications", [])

    def wait(self, app_id: str, timeout: int = 300) -> str:
        deadline, status = time.time() + timeout, ""
        while time.time() < deadline:
            status = self.call("GET", f"{self.apps_path}/{app_id}")["status"]
            if status in TERMINAL:
                return status
            time.sleep(5)
        return status


def cmd_list(api: Api, _: argparse.Namespace) -> None:
    for a in api.apps():
        e = env_of(a)
        print(
            f"{a['subdomain']:<12} {a['status']:<22} run_as={a.get('run_as', 0):<4} "
            f"health={e.get('CDSW_APP_POLLING_ENDPOINT', '-'):<12} {a['name']}"
        )


def cmd_env(api: Api, args: argparse.Namespace) -> None:
    env = env_of(find_app(api.apps(), args.app))
    for k, v in sorted((env if args.reveal else mask(env)).items()):
        print(f"{k}={v}")


def cmd_set(api: Api, args: argparse.Namespace) -> None:
    app = find_app(api.apps(), args.app)
    env = env_of(app)
    changes, removals = parse_assignments(args.assignments), args.unset or []
    env.update(changes)
    for k in removals:
        env.pop(k, None)
    shown = {**mask(changes), **{k: "<removed>" for k in removals}}
    print(f"{app['subdomain']}: {json.dumps(shown)}")
    if args.dry_run:
        return
    # `environment` is a JSON-encoded *string*, and it replaces the whole value: always send the full dict.
    api.call("PATCH", f"{api.apps_path}/{app['id']}", {"environment": json.dumps(env)})
    print("updated; restart the app for it to take effect" if not args.restart else "updated")
    if args.restart:
        restart(api, app)


def restart(api: Api, app: dict) -> None:
    api.call("POST", f"{api.apps_path}/{app['id']}:restart", {})
    print(f"{app['subdomain']}: restarting ... {api.wait(app['id'])}")


def cmd_restart(api: Api, args: argparse.Namespace) -> None:
    restart(api, find_app(api.apps(), args.app))


def cmd_stop(api: Api, args: argparse.Namespace) -> None:
    app = find_app(api.apps(), args.app)
    api.call("POST", f"{api.apps_path}/{app['id']}:stop", {})
    print(f"{app['subdomain']}: stopping ... {api.wait(app['id'])}")


def cmd_run_as(api: Api, args: argparse.Namespace) -> None:
    app = find_app(api.apps(), args.app)
    api.call("PATCH", f"{api.apps_path}/{app['id']}", {"run_as": args.user_id})
    print(f"{app['subdomain']}: run_as={args.user_id}")


def resolve_runtime(api: Api, match: str) -> str:
    runtimes = api.call("GET", "/runtimes?page_size=200").get("runtimes", [])
    ids = sorted(
        r["image_identifier"] for r in runtimes if match in r["image_identifier"] and r.get("status") == "ENABLED"
    )
    if not ids:
        sys.exit(f"No enabled runtime matches {match!r}")
    return ids[-1]


def cmd_create(api: Api, args: argparse.Namespace) -> None:
    manifest = json.loads(MANIFEST.read_text())
    existing = {a["subdomain"] for a in api.apps()}
    missing: set[str] = set()
    specs = []
    for spec in manifest["apps"]:
        env = {k: expand(v, dict(os.environ), missing) for k, v in spec["env"].items()}
        specs.append({**{k: v for k, v in spec.items() if k != "env"}, "environment": env})
    if missing:
        sys.exit(f"Set these in your environment first: {', '.join(sorted(missing))}")
    runtime = resolve_runtime(api, manifest["runtime_match"])
    for spec in specs:
        if spec["subdomain"] in existing:
            print(f"{spec['subdomain']}: exists, skipping (use `set` to change it)")
            continue
        body = {
            **spec,
            "project_id": api.project,
            "kernel": "",
            "runtime_identifier": runtime,
            "bypass_authentication": False,
        }
        if args.dry_run:
            print(f"{spec['subdomain']}: would create {json.dumps({**body, 'environment': mask(spec['environment'])})}")
            continue
        app = api.call("POST", api.apps_path, body)
        # CAI starts an application the moment it is created. Stop it unless asked otherwise.
        if args.start:
            print(f"{spec['subdomain']}: created ... {api.wait(app['id'])}")
        else:
            api.call("POST", f"{api.apps_path}/{app['id']}:stop", {})
            print(f"{spec['subdomain']}: created, stopped ... {api.wait(app['id'])}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="applications, status, run-as and health path").set_defaults(fn=cmd_list)
    s = sub.add_parser("env", help="show an application's environment (secrets masked)")
    s.add_argument("app")
    s.add_argument("--reveal", action="store_true", help="print secret values too")
    s.set_defaults(fn=cmd_env)
    s = sub.add_parser("set", help="add/change/remove environment variables")
    s.add_argument("app")
    s.add_argument("assignments", nargs="*", metavar="KEY=VALUE|KEY=@file")
    s.add_argument("--unset", nargs="+", metavar="KEY")
    s.add_argument("--restart", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_set)
    for name, fn in (("restart", cmd_restart), ("stop", cmd_stop)):
        s = sub.add_parser(name)
        s.add_argument("app")
        s.set_defaults(fn=fn)
    s = sub.add_parser("run-as", help="run an app as a project machine user")
    s.add_argument("app")
    s.add_argument("user_id", type=int)
    s.set_defaults(fn=cmd_run_as)
    s = sub.add_parser("create", help="create the apps in deploy/apps.json that don't exist yet")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--start", action="store_true", help="leave them running (default: stop right after creation)")
    s.set_defaults(fn=cmd_create)
    args = p.parse_args()
    args.fn(Api(), args)


if __name__ == "__main__":
    main()
