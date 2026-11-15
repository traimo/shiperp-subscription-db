"""On Azure, data/ must be the Azure Files share: banner on every page and a /healthz field when it is not
(app.config.data_mount_problem). Off Azure (no CONTAINER_APP_NAME) nothing changes."""
import importlib.util
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import config, main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_check():
    config.data_mount_problem.cache_clear()
    yield
    config.data_mount_problem.cache_clear()


def test_off_azure_no_banner_and_plain_healthz(monkeypatch):
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setattr(config.os.path, "ismount", lambda p: False)
    c = TestClient(main.app)
    assert config.data_mount_problem() is None
    assert c.get("/healthz").json() == {"status": "ok"}
    assert "Data folder not mounted" not in c.get("/check").text


def test_on_azure_without_the_share_banner_and_healthz_missing(monkeypatch):
    monkeypatch.setenv("CONTAINER_APP_NAME", "shiperp-subscription-db-app")
    monkeypatch.setattr(config.os.path, "ismount", lambda p: False)
    c = TestClient(main.app)
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "data_mount": "missing"}   # never fails the probe
    html = c.get("/check").text
    assert "Data folder not mounted" in html and "azure_mount_data.py" in html


def test_on_azure_with_the_share_no_banner(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTAINER_APP_NAME", "shiperp-subscription-db-app")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config.os.path, "ismount", lambda p: Path(p) == tmp_path)
    c = TestClient(main.app)
    assert config.data_mount_problem() is None
    assert c.get("/healthz").json() == {"status": "ok", "data_mount": "ok"}
    assert "Data folder not mounted" not in c.get("/check").text
    assert list(tmp_path.iterdir()) == []                         # the write probe cleans up after itself


def test_mounted_but_not_writable(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTAINER_APP_NAME", "x")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "missing-dir")          # writing into it fails
    monkeypatch.setattr(config.os.path, "ismount", lambda p: True)
    assert "not writable" in config.data_mount_problem()


def test_compose_declares_the_share_the_mount_script_uses():
    """compose.yaml's x-azure.azure_files (read by az-deploy-agent) and scripts/azure_mount_data.py (manual fallback)
    must describe the same mount."""
    spec = importlib.util.spec_from_file_location("azure_mount_data", ROOT / "scripts" / "azure_mount_data.py")
    mount = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mount)
    app_svc = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]["app"]
    assert "volumes" not in app_svc                                   # the deployer rejects compose volumes
    [entry] = app_svc["x-azure"]["azure_files"]
    assert entry["storage"] == mount.STORAGE and entry["target"] == mount.MOUNT == str(config.DATA_DIR).replace(
        str(config.PROJECT_DIR), "/app").replace("\\", "/")
    built = mount.with_mount({"containers": [{"name": "app"}], "volumes": []})["volumes"][0]
    assert built["mountOptions"] == entry["mount_options"]
