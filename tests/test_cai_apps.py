import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "cai_apps", Path(__file__).resolve().parents[1] / "scripts" / "cai_apps.py"
)
cai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cai)


def test_mask_hides_only_secret_names():
    env = {
        "ZQ_SERVICE_TOKEN": "abc",
        "OPENROUTER_API_KEY": "k" * 10,
        "ZQ_KEYTAB": "keytabs/x",
        "ZQ_WAREHOUSE": "impala",
    }
    masked = cai.mask(env)
    assert masked["ZQ_SERVICE_TOKEN"] == "<set>" and masked["OPENROUTER_API_KEY"] == "<set>"
    # ZQ_KEYTAB contains "KEY" but is not a secret value; only the suffix counts.
    assert masked["ZQ_KEYTAB"] == "keytabs/x" and masked["ZQ_WAREHOUSE"] == "impala"


def test_parse_assignments_reads_file_values(tmp_path):
    secret = tmp_path / "tok"
    secret.write_text("s3cret\n")
    assert cai.parse_assignments(["A=1", f"B=@{secret}", "C=x=y"]) == {"A": "1", "B": "s3cret", "C": "x=y"}
    with pytest.raises(SystemExit):
        cai.parse_assignments(["no-equals"])


def test_expand_uses_env_then_default_then_reports_missing():
    missing: set[str] = set()
    env = {"HOST": "h"}
    assert cai.expand("${HOST}:${PORT:-443}/${NOPE}", env, missing) == "h:443/${NOPE}"
    assert missing == {"NOPE"}


def test_find_app_by_subdomain_or_name():
    apps = [
        {"id": "1", "name": "Zero Query", "subdomain": "zq"},
        {"id": "2", "name": "Zero Query MCP", "subdomain": "zq-mcp"},
    ]
    assert cai.find_app(apps, "zq-mcp")["id"] == "2"
    assert cai.find_app(apps, "zero query mcp")["id"] == "2"
    with pytest.raises(SystemExit):
        cai.find_app(apps, "nope")


def test_manifest_has_no_secrets_or_hostnames():
    text = (Path(__file__).resolve().parents[1] / "deploy" / "apps.json").read_text()
    assert "TOKEN" not in text.replace("ZQ_SERVICE_TOKEN", "") and "API_KEY" not in text.replace(
        "OPENROUTER_API_KEY", ""
    )
    assert "://" not in text
