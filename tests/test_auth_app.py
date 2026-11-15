"""Sign-in wired into the real app (app/main.py): its own middleware stack (session -> sign-in guard -> host check ->
same-origin posts), the header with the user's name, and the audit actor. The other page tests run with sign-in off
(conftest), so a fresh copy of app.main is loaded here with sign-in on."""
import importlib.util
import sys

import pytest
from fastapi.testclient import TestClient

from app import auth
from tests.test_auth import GROUP, KEY, PAGE, FakeMsal


@pytest.fixture
def signin_main(monkeypatch, blank):
    env = {"AUTH_DISABLED": "0", "ENTRA_TENANT_ID": "tid", "ENTRA_CLIENT_ID": "cid", "ENTRA_CLIENT_SECRET": "sec",
           "REDIRECT_URI": "http://testserver/auth/callback", "SESSION_SECRET_KEY": KEY, "ALLOWED_GROUP": GROUP,
           "ALLOWED_USERS": "", "APP_ALLOWED_HOSTS": ""}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(auth, "_msal_app", lambda c: FakeMsal())
    name = "app._main_signin_test"
    spec = importlib.util.spec_from_file_location(name, auth.__file__.replace("auth.py", "main.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        mod.SESSION_FACTORY = blank
        yield mod
    finally:
        sys.modules.pop(name, None)


def test_real_app_requires_sign_in_and_shows_the_user(signin_main):
    assert not signin_main.AUTH.auth_disabled
    c = TestClient(signin_main.app)
    assert c.get("/healthz").json() == {"status": "ok"}
    r = c.get("/", headers=PAGE, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.post("/refresh", headers={"x-requested-with": "fetch"}).status_code == 401

    FakeMsal.claims = {"preferred_username": "Member@erp-is.com", "name": "Mia Member", "oid": "o1", "groups": [GROUP]}
    c.get("/auth/start", follow_redirects=False)
    assert c.get("/auth/callback?code=abc&state=s", follow_redirects=False).status_code == 303
    page = c.get("/", headers=PAGE)
    assert page.status_code == 200 and "Mia Member" in page.text and 'href="/logout"' in page.text
    assert signin_main.actor() != "member@erp-is.com"          # only inside a request
    r = c.post("/refresh", headers={**PAGE, "origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and "another site" in r.text    # same-origin check still runs when signed in


def test_real_app_host_check_uses_the_shared_host_list(signin_main):
    c = TestClient(signin_main.app, base_url="http://subs.other.example")
    assert c.get("/healthz").status_code == 200                 # probes by any name
    r = c.get("/login")
    assert r.status_code == 400                                  # not in APP_ALLOWED_HOSTS
