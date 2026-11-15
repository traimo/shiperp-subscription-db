"""Microsoft sign-in (app/auth.py): guard, Entra callback with a stubbed MSAL, allow-list, startup guards, actor."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth

GROUP = "11111111-2222-3333-4444-555555555555"
KEY = "k" * 64
PAGE = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}     # what a browser sends for a page / form post


def cfg(**kw) -> auth.Settings:
    base = dict(tenant_id="tid", client_id="cid", client_secret="sec", redirect_uri="http://testserver/auth/callback",
                allowed_group=GROUP, allowed_users={"allowed@erp-is.com"}, secret_key=KEY)
    base.update(kw)
    return auth.Settings(**base)


class FakeMsal:
    claims: dict = {}
    built = 0

    def __init__(self):
        FakeMsal.built += 1

    def initiate_auth_code_flow(self, scopes, redirect_uri):
        return {"auth_uri": "https://login.microsoftonline.com/tid/authorize?x=1", "state": "s"}

    def acquire_token_by_auth_code_flow(self, flow, params):
        if params.get("state") != flow["state"]:
            raise ValueError("state mismatch")
        return {"id_token_claims": FakeMsal.claims}


def make_client(monkeypatch, settings: auth.Settings, claims=None) -> TestClient:
    from app import main
    FakeMsal.claims = claims or {}
    FakeMsal.built = 0
    monkeypatch.setattr(auth, "_msal_app", lambda c: FakeMsal())
    app = FastAPI()
    auth.install(app, settings)

    @app.get("/")
    def home():
        return {"ok": True}

    @app.get("/who")
    def who():
        return {"actor": main.actor()}

    @app.post("/change")
    def change():
        return {"actor": main.actor()}

    return TestClient(app)


def sign_in(c: TestClient):
    r = c.get("/auth/start?next=/who", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://login.microsoftonline.com/")
    return c.get("/auth/callback?code=abc&state=s", follow_redirects=False)


# ------------------------------------------------------------------ access decision / helpers

def test_access_decision_group_and_allow_list():
    s = cfg()
    assert auth.access_decision(s, {"preferred_username": "Allowed@ERP-IS.com"}) == (True, "allow-list")
    assert auth.access_decision(s, {"preferred_username": "x@erp-is.com", "groups": [GROUP.upper()]})[0]
    ok, why = auth.access_decision(s, {"preferred_username": "x@erp-is.com", "groups": ["other"]})
    assert not ok and "not a member" in why
    ok, why = auth.access_decision(s, {"preferred_username": "x@erp-is.com"})
    assert not ok and "groupMembershipClaims" in why
    ok, _ = auth.access_decision(cfg(allowed_group="", allowed_users=set()), {"preferred_username": "x@erp-is.com"})
    assert not ok


@pytest.mark.parametrize("target,expected", [
    ("/customer/80232", "/customer/80232"), ("/check?status=open", "/check?status=open"), (None, "/"),
    ("https://evil.com/", "/"), ("//evil.com", "/"), ("/\\evil.com", "/"), ("customers", "/"), ("/a\r\nSet-Cookie:x", "/"),
])
def test_safe_next(target, expected):
    assert auth.safe_next(target) == expected


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", " A@erp-is.com , b@erp-is.com ")
    monkeypatch.setenv("ALLOWED_GROUP", GROUP.upper())
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("APP_ALLOWED_HOSTS", "subs.example.internal")
    s = auth.Settings.from_env()
    assert s.allowed_users == {"a@erp-is.com", "b@erp-is.com"} and s.allowed_group == GROUP
    assert s.auth_disabled and s.exposed


# ------------------------------------------------------------------ startup guards

def test_refuses_auth_disabled_when_reachable_from_elsewhere():
    with pytest.raises(RuntimeError, match="AUTH_DISABLED"):
        auth.startup_guards(cfg(auth_disabled=True, allowed_hosts=["subs.bravesmoke.westus2.azurecontainerapps.io"]))
    auth.startup_guards(cfg(auth_disabled=True, allowed_hosts=["127.0.0.1", "localhost"]))   # loopback is fine


def test_refuses_sign_in_without_session_key():
    with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY"):
        auth.startup_guards(cfg(secret_key=""))
    with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY"):
        auth.startup_guards(cfg(secret_key="short"))
    auth.startup_guards(cfg(auth_disabled=True, secret_key=""))     # local, no sign-in: a random key is used


# ------------------------------------------------------------------ guard and flow

def test_pages_redirect_to_login_when_signed_out(monkeypatch):
    c = make_client(monkeypatch, cfg())
    r = c.get("/who", headers=PAGE, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fwho"
    assert c.get("/", headers=PAGE, follow_redirects=False).headers["location"] == "/login"
    assert c.post("/change").status_code == 401               # scripts / API calls: 401, no redirect
    r = c.get("/login")
    assert r.status_code == 200 and "Sign in with Microsoft" in r.text


def test_healthz_and_static_stay_public(monkeypatch):
    c = make_client(monkeypatch, cfg())
    assert auth.is_public("/healthz") and auth.is_public("/static/app.js") and auth.is_public("/auth/callback")
    assert not auth.is_public("/healthzz") and not auth.is_public("/db") and not auth.is_public("/loginx")
    assert c.get("/healthz", follow_redirects=False).status_code == 404   # public: reaches routing, not /login


def test_sign_in_sets_actor_to_the_users_email(monkeypatch):
    c = make_client(monkeypatch, cfg(), {"preferred_username": "Member@erp-is.com", "name": "M", "groups": [GROUP]})
    r = sign_in(c)
    assert r.status_code == 303 and r.headers["location"] == "/who"
    assert c.get("/who").json() == {"actor": "member@erp-is.com"}
    assert c.post("/change").json() == {"actor": "member@erp-is.com"}
    c.get("/logout", follow_redirects=False)
    assert c.get("/who", headers=PAGE, follow_redirects=False).status_code == 303
    assert FakeMsal.built == 1                                 # one MSAL client for start + callback


def test_denied_user_gets_403_and_no_session(monkeypatch):
    c = make_client(monkeypatch, cfg(), {"preferred_username": "outsider@erp-is.com", "groups": ["other"]})
    r = sign_in(c)
    assert r.status_code == 403 and "Not authorised" in r.text and "outsider@erp-is.com" in r.text
    assert c.get("/who", headers=PAGE, follow_redirects=False).status_code == 303


def test_callback_without_flow_or_with_bad_state(monkeypatch):
    c = make_client(monkeypatch, cfg(), {"preferred_username": "allowed@erp-is.com"})
    r = c.get("/auth/callback?code=x&state=s", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?error=expired"
    assert "session expired" in c.get("/login?error=expired").text
    c.get("/auth/start", follow_redirects=False)
    r = c.get("/auth/callback?code=x&state=forged", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?error=verify"
    assert "could not be verified" in c.get("/login?error=verify").text


def test_login_page_shows_only_fixed_error_messages(monkeypatch):
    c = make_client(monkeypatch, cfg())
    page = c.get("/login?error=Your+account+is+locked,+call+555-0100").text
    assert "555-0100" not in page and 'class="err"' not in page


def test_expired_session_scripts_get_401_and_forms_go_back_to_sign_in(monkeypatch):
    c = make_client(monkeypatch, cfg())
    r = c.get("/who", headers={"x-requested-with": "fetch", "accept": "*/*"}, follow_redirects=False)
    assert r.status_code == 401 and r.json()["login"] == "/login"           # fetch: no login page in a dialog
    r = c.post("/change", headers={**PAGE, "referer": "http://testserver/customer/80232?tab=terms"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fcustomer%2F80232%3Ftab%3Dterms"
    r = c.post("/change", headers={**PAGE, "referer": "https://evil.example/x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"     # foreign referer: back to the start page


def test_actor_without_email_claim_is_the_object_id_not_the_os_user(monkeypatch):
    assert auth.user_actor({"email": "", "oid": "abc-123", "name": "N"}) == "oid:abc-123"
    assert auth.user_actor({"email": "a@erp-is.com", "oid": "abc"}) == "a@erp-is.com"
    c = make_client(monkeypatch, cfg(), {"oid": "abc-123", "name": "Guest", "groups": [GROUP]})
    sign_in(c)
    assert c.get("/who").json() == {"actor": "oid:abc-123"}


def test_not_configured_login_page_says_so(monkeypatch):
    c = make_client(monkeypatch, cfg(tenant_id=""))
    r = c.get("/login")
    assert r.status_code == 503 and "not configured" in r.text


def test_auth_disabled_falls_back_to_app_user(monkeypatch):
    monkeypatch.setenv("APP_USER", "local.dev")
    c = make_client(monkeypatch, cfg(auth_disabled=True, secret_key=""))
    assert c.get("/who").json() == {"actor": "local.dev"}
    assert c.get("/login", follow_redirects=False).headers["location"] == "/"


def test_host_list_has_one_parser(monkeypatch):
    monkeypatch.setenv("APP_ALLOWED_HOSTS", " subs.example.internal , ,other ")
    assert auth.allowed_hosts_from_env() == ["subs.example.internal", "other"]
    assert auth.Settings.from_env().allowed_hosts == auth.allowed_hosts_from_env()


def test_main_app_is_installed_with_sign_in_off_for_tests():
    from app import main
    assert main.AUTH.auth_disabled                 # conftest: page tests run without sign-in
    assert TestClient(main.app).get("/healthz").json() == {"status": "ok"}
