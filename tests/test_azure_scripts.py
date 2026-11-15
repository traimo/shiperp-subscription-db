"""scripts/azure_secrets.py and scripts/azure_mount_data.py: no Azure calls, only the pure parts."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


secrets_script = load("azure_secrets")
mount = load("azure_mount_data")


# ------------------------------------------------------------------ secrets

def test_script_covers_exactly_the_compose_secrets():
    assert secrets_script.compose_secret_names() == set(secrets_script.ALL)


def test_kv_names():
    assert secrets_script.kv_name("SESSION_SECRET_KEY") == "session-secret-key"


def test_collect_builds_values_and_reports_missing(tmp_path):
    env = {n: f"v-{n}" for n in secrets_script.FROM_ENV} | {"AZ_PG_SUBS_PASSWORD": "pw123"}
    dash = {"ENTRA_TENANT_ID": "t", "ENTRA_CLIENT_ID": "c"}            # secret missing
    values, missing = secrets_script.collect(env, dash, "subs.example.io")
    assert missing == ["ENTRA_CLIENT_SECRET"]
    assert values["DATABASE_URL"] == ("postgresql+psycopg://subs:pw123@psql-shiperp-subs.postgres.database.azure.com:"
                                      "5432/subscriptions?sslmode=require")
    assert values["REDIRECT_URI"] == "https://subs.example.io/auth/callback"
    assert len(values["SESSION_SECRET_KEY"]) == 64
    assert secrets_script.collect(env, dash, "x")[0]["SESSION_SECRET_KEY"] != values["SESSION_SECRET_KEY"]


def test_read_env_skips_comments_and_blanks(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# A=1\nB=\nC = "x=y"\n  D=2\n', encoding="utf-8")
    assert secrets_script.read_env(p) == {"C": "x=y", "D": "2"}


def test_dry_run_never_prints_values(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("".join(f"{n}=ZZVAL{n}\n" for n in secrets_script.FROM_ENV) + "AZ_PG_SUBS_PASSWORD=ZZVALPW\n",
                   encoding="utf-8")
    dash = tmp_path / "dash.env"
    dash.write_text("".join(f"{n}=ZZVAL{n}\n" for n in secrets_script.FROM_DASHBOARD), encoding="utf-8")
    assert secrets_script.main(["--dry-run", "--env-file", str(env), "--dashboard-env", str(dash)]) == 0
    out = capsys.readouterr().out
    assert "ZZVAL" not in out and "database-url" in out and "MISSING" not in out


# ------------------------------------------------------------------ mount

TEMPLATE = {"containers": [{"name": "app", "image": "crshiperpagents.azurecr.io/x:1", "env": [{"name": "A", "value": "1"}]}],
            "volumes": [], "scale": {"minReplicas": 1, "maxReplicas": 1}}


def test_mount_added_once_and_other_settings_kept():
    assert not mount.is_mounted(TEMPLATE)
    t = mount.with_mount(TEMPLATE)
    assert mount.is_mounted(t) and TEMPLATE["volumes"] == []                    # input not modified
    assert t["containers"][0]["env"] == [{"name": "A", "value": "1"}] and t["scale"] == TEMPLATE["scale"]
    again = mount.with_mount(t)
    assert len(again["volumes"]) == 1 and len(again["containers"][0]["volumeMounts"]) == 1
    assert "uid=10001" in again["volumes"][0]["mountOptions"]


def test_mount_keeps_secret_file_volumes():
    t = {"containers": [{"name": "app", "volumeMounts": [{"volumeName": "secrets", "mountPath": "/run/secrets"}]}],
         "volumes": [{"name": "secrets", "storageType": "Secret"}]}
    out = mount.with_mount(t)
    assert {v["name"] for v in out["volumes"]} == {"secrets", "subs-data"}
    assert {m["mountPath"] for m in out["containers"][0]["volumeMounts"]} == {"/run/secrets", "/app/data"}


@pytest.mark.parametrize("bad", [
    {"containers": [{"volumeMounts": [{"volumeName": "subs-data", "mountPath": "/app/data"}]}],
     "volumes": [{"name": "subs-data", "storageType": "EmptyDir"}]},                 # scratch disk, not the share
    {"containers": [{"volumeMounts": [{"volumeName": "subs-data", "mountPath": "/data"}]}],
     "volumes": [{"name": "subs-data", "storageType": "AzureFile", "storageName": "subs-data"}]},   # wrong path
])
def test_is_mounted_requires_the_share_on_app_data(bad):
    assert not mount.is_mounted(bad)
