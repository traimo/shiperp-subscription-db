"""Microsoft Entra ID sign-in, the same flow as shiperp-dashboard (app/auth.py there), for this FastAPI app.

Flow: /login (page) -> /auth/start (MSAL authorization-code flow) -> Microsoft -> /auth/callback (token exchange,
allow-list check) -> signed session cookie. Only name, e-mail and object id are kept in the cookie; no tokens.
Access: members of ALLOWED_GROUP (the ID token's `groups` claim) and/or e-mails on ALLOWED_USERS (case-insensitive).

The signed-in e-mail is the audit-log actor (`user_actor`, read by main.actor()). AUTH_DISABLED=1 turns sign-in off for local use only:
the app refuses to start with it when APP_ALLOWED_HOSTS names any non-loopback host (i.e. it is reachable from
elsewhere), and refuses to start with sign-in on but no SESSION_SECRET_KEY.

Settings (environment): ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET, REDIRECT_URI, ALLOWED_GROUP,
ALLOWED_USERS, SESSION_SECRET_KEY, AUTH_DISABLED.
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass, field
from html import escape
from urllib.parse import quote, urlencode, urlparse, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

log = logging.getLogger(__name__)

SCOPES = ["User.Read"]
PUBLIC_PREFIXES = ("/login", "/auth/", "/logout", "/static/", "/healthz")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}

# /login?error=<code>: only these fixed messages are shown (the link cannot put its own text on the sign-in page).
LOGIN_ERRORS = {
    "expired": "Your sign-in session expired. Please sign in again.",
    "verify": "Sign-in could not be verified. Please try again.",
    "microsoft": "Microsoft sign-in did not complete. Please try again; if it keeps failing, ask Tony.",
}

# The signed-in user's e-mail for the current request (read by main.actor()); None when sign-in is disabled.
CURRENT_EMAIL: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_email", default=None)


def _split(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def allowed_hosts_from_env() -> list[str]:
    """APP_ALLOWED_HOSTS as a list: the one parser for both the host check (main.py) and the startup guard here."""
    return _split(os.environ.get("APP_ALLOWED_HOSTS", ""))


@dataclass
class Settings:
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    allowed_group: str = ""
    allowed_users: set[str] = field(default_factory=set)
    secret_key: str = ""
    auth_disabled: bool = False
    allowed_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(tenant_id=e("ENTRA_TENANT_ID", "").strip(), client_id=e("ENTRA_CLIENT_ID", "").strip(),
                   client_secret=e("ENTRA_CLIENT_SECRET", "").strip(), redirect_uri=e("REDIRECT_URI", "").strip(),
                   allowed_group=e("ALLOWED_GROUP", "").strip().lower(),
                   allowed_users={u.lower() for u in _split(e("ALLOWED_USERS", ""))},
                   secret_key=e("SESSION_SECRET_KEY", "").strip(),
                   auth_disabled=e("AUTH_DISABLED", "").strip().lower() in ("1", "true", "yes"),
                   allowed_hosts=allowed_hosts_from_env())

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret and self.redirect_uri)

    @property
    def access_policy(self) -> str:
        """A fingerprint of who may sign in (ALLOWED_GROUP + ALLOWED_USERS), stored in each session at sign-in. When
        either setting changes, existing sessions no longer match and must sign in again, so the new rule applies at
        once instead of after the 12-hour session ends."""
        raw = self.allowed_group + "|" + ",".join(sorted(self.allowed_users))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def exposed(self) -> bool:
        """Reachable under a name other than this machine's loopback (APP_ALLOWED_HOSTS lists one)."""
        return any(h.lower() not in LOOPBACK_HOSTS for h in self.allowed_hosts)


def startup_guards(cfg: Settings) -> None:
    """Refuse to run reachable from elsewhere without sign-in, or with sign-in but no session key."""
    if cfg.auth_disabled and cfg.exposed:
        raise RuntimeError("AUTH_DISABLED=1 is only allowed when the app is reachable on this machine alone; "
                           f"APP_ALLOWED_HOSTS lists {', '.join(cfg.allowed_hosts)}. Remove AUTH_DISABLED (and set "
                           "the ENTRA_* settings) or remove the extra host names.")
    if not cfg.auth_disabled and len(cfg.secret_key) < 32:
        raise RuntimeError("SESSION_SECRET_KEY must be set (32+ characters) when sign-in is on - generate one with "
                           "python -c \"import secrets; print(secrets.token_hex(32))\".")


def session_secret(cfg: Settings) -> str:
    """The cookie-signing key: SESSION_SECRET_KEY, or a per-process random key when sign-in is off locally."""
    return cfg.secret_key or secrets.token_hex(32)


def claims_email(claims: dict) -> str:
    return (claims.get("preferred_username") or claims.get("email") or claims.get("upn") or "").lower()


def access_decision(cfg: Settings, claims: dict) -> tuple[bool, str]:
    """(allowed, reason). Allowed when the ID token's `groups` claim contains ALLOWED_GROUP, or the e-mail is in
    ALLOWED_USERS. With a group configured but no `groups` claim, the Entra app is missing groupMembershipClaims =
    SecurityGroup (or the user is in too many groups): deny and say so."""
    email = claims_email(claims)
    if email and email in cfg.allowed_users:
        return True, "allow-list"
    if cfg.allowed_group:
        groups = claims.get("groups")
        if groups is None:
            return False, ("the sign-in token carries no group membership (the Entra app needs groupMembershipClaims "
                           "= SecurityGroup), so group access cannot be checked")
        if cfg.allowed_group in {str(g).lower() for g in groups}:
            return True, "group"
        return False, "is not a member of the subscription database access group"
    return False, "is not on the access list for the subscription database"


def safe_next(target: str | None) -> str:
    """A same-site redirect target: `target` when it is a plain path, else '/'. A backslash reads as a slash in a
    Location header ('/\\evil.com' -> '//evil.com') and control characters could split the header: both refused."""
    if not target:
        return "/"
    u = urlparse(target)
    if (u.scheme or u.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in target)):
        return "/"
    return target


def is_public(path: str) -> bool:
    """Exact match for page entries (/login, /healthz ...); prefix match only for folder entries ending in '/'."""
    return any(path.startswith(p) if p.endswith("/") else path == p for p in PUBLIC_PREFIXES)


def _msal_app(cfg: Settings):
    import msal
    return msal.ConfidentialClientApplication(cfg.client_id, authority=cfg.authority,
                                              client_credential=cfg.client_secret)


def logout_url(cfg: Settings) -> str:
    post = cfg.redirect_uri.rsplit("/auth/callback", 1)[0] + "/login"
    return f"{cfg.authority}/oauth2/v2.0/logout?post_logout_redirect_uri={quote(post, safe='')}"


_STYLE = """:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--muted:#5d6b7a;--accent:#1d4ed8;--border:#e3e7ec}
@media (prefers-color-scheme:dark){:root{--bg:#0f1419;--card:#171d25;--ink:#e8edf2;--muted:#9aa7b4;--accent:#60a5fa;
--border:#2a323c}}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,Segoe UI,Roboto,sans-serif;display:grid;
place-items:center;min-height:100vh}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:40px 44px;max-width:420px;
width:calc(100% - 32px);box-shadow:0 10px 30px rgba(0,0,0,.08);text-align:center}
h1{font-size:22px;margin:0 0 6px}p{color:var(--muted);margin:0 0 26px}a{color:var(--accent)}
a.btn{display:inline-flex;align-items:center;gap:10px;background:var(--accent);color:#fff;text-decoration:none;
padding:12px 20px;border-radius:10px;font-weight:600}a.btn svg{width:18px;height:18px}
.err{color:#b42318;background:#fee4e2;border-radius:8px;padding:10px 12px;margin-bottom:18px;font-size:14px}
small{display:block;margin-top:22px;color:var(--muted);font-size:12px}"""

_MS_LOGO = ('<svg viewBox="0 0 23 23" aria-hidden="true"><path fill="#f3f3f3" d="M0 0h23v23H0z"/><path fill="#f35325" '
            'd="M1 1h10v10H1z"/><path fill="#81bc06" d="M12 1h10v10H12z"/><path fill="#05a6f0" d="M1 12h10v10H1z"/>'
            '<path fill="#ffba08" d="M12 12h10v10H12z"/></svg>')


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>{escape(title)}</title><style>{_STYLE}</style>'
            f'</head><body><div class="card">{body}</div></body></html>')


def login_page(start_url: str, error: str = "") -> str:
    err = f'<div class="err">{escape(error)}</div>' if error else ""
    return _page("Subscription Database", f"<h1>Subscription Database</h1><p>Sign in with your ERP-IS Microsoft "
                 f'account.</p>{err}<a class="btn" href="{escape(start_url)}">{_MS_LOGO}Sign in with Microsoft</a>'
                 "<small>Access is limited to authorised finance users.</small>")


def denied_page(email: str, reason: str, logout: str) -> str:
    return _page("Not authorised", f"<h1>Not authorised</h1><p>{escape(email)} is signed in but {escape(reason)}.</p>"
                 f'<p><a href="{escape(logout)}">Sign out</a> and try another account, or ask Tony to add you.</p>')


def current_user(request: Request) -> dict | None:
    return request.session.get("user")


def user_actor(user: dict) -> str | None:
    """The audit-log name of a signed-in user: the e-mail, else the Entra object id (never the container's OS user)."""
    return user.get("email") or (f"oid:{user['oid']}" if user.get("oid") else None) or user.get("name") or None


def _return_path(request: Request) -> str:
    """Where to go back to after signing in: the page a form was posted from (same host only), else '/'."""
    ref = urlsplit(request.headers.get("referer", ""))
    if ref.netloc and ref.netloc == request.headers.get("host", ""):
        return safe_next(ref.path + (f"?{ref.query}" if ref.query else ""))
    return "/"


def _login_redirect(nxt: str) -> RedirectResponse:
    return RedirectResponse("/login" + (f"?{urlencode({'next': nxt})}" if nxt != "/" else ""), status_code=303)


def install(app, cfg: Settings) -> None:
    """Add the sign-in guard, the session cookie and the /login, /auth/*, /logout routes to `app`.
    Starlette runs the last-added middleware first, so the session middleware (added last) wraps the guard."""
    from starlette.middleware.sessions import SessionMiddleware

    startup_guards(cfg)
    https = cfg.redirect_uri.startswith("https://")
    _client: list = []

    def msal_client():
        """One MSAL client per process (building one fetches Entra's OpenID metadata over the network)."""
        if not _client:
            _client.append(_msal_app(cfg))
        return _client[0]

    @app.middleware("http")
    async def sign_in_guard(request: Request, call_next):
        path = request.url.path
        if cfg.auth_disabled or is_public(path):
            return await call_next(request)
        user = current_user(request)
        if user and user.get("policy") != cfg.access_policy:
            # Signed in under an earlier access rule (another group/allow-list, or before policies were recorded):
            # sign in again so the current rule decides.
            log.info("session for oid %s predates the current access rule; signing in again", user.get("oid"))
            request.session.clear()
            user = None
        if not user:
            accept = request.headers.get("accept", "")
            # Scripts (fetch: Accept */* or JSON, or our X-Requested-With) get 401 and handle it themselves.
            if "application/json" in accept or request.headers.get("x-requested-with") or "text/html" not in accept:
                return JSONResponse({"error": "not signed in", "login": "/login"}, status_code=401)
            if request.method in ("GET", "HEAD"):
                return _login_redirect(path + (f"?{request.url.query}" if request.url.query else ""))
            # A browser form posted after the session expired: back to sign-in, then to the page it came from
            # (the unsaved entry is lost, but the user is not left on an error page).
            return _login_redirect(_return_path(request))
        token = CURRENT_EMAIL.set(user_actor(user))
        try:
            return await call_next(request)
        finally:
            CURRENT_EMAIL.reset(token)

    app.add_middleware(SessionMiddleware, secret_key=session_secret(cfg), session_cookie="subs_session",
                       max_age=12 * 3600, same_site="lax", https_only=https)

    @app.get("/login", include_in_schema=False)
    def auth_login(request: Request, next: str = "", error: str = ""):
        if cfg.auth_disabled or current_user(request):
            return RedirectResponse("/", status_code=303)
        if not cfg.configured:
            return HTMLResponse(login_page("#", "Entra is not configured: set ENTRA_TENANT_ID, ENTRA_CLIENT_ID, "
                                           "ENTRA_CLIENT_SECRET and REDIRECT_URI."), status_code=503)
        start = "/auth/start" + (f"?{urlencode({'next': safe_next(next)})}" if next else "")
        return HTMLResponse(login_page(start, LOGIN_ERRORS.get(error, "")))

    @app.get("/auth/start", include_in_schema=False)
    def auth_start(request: Request, next: str = ""):
        if not cfg.configured:
            return HTMLResponse("Sign-in is not configured.", status_code=503)
        flow = msal_client().initiate_auth_code_flow(SCOPES, redirect_uri=cfg.redirect_uri)
        request.session["auth_flow"] = flow
        request.session["next"] = safe_next(next)
        return RedirectResponse(flow["auth_uri"], status_code=303)

    @app.get("/auth/callback", include_in_schema=False)
    def auth_callback(request: Request):
        flow = request.session.pop("auth_flow", None)
        if not flow:
            return RedirectResponse("/login?error=expired", status_code=303)
        try:
            result = msal_client().acquire_token_by_auth_code_flow(flow, dict(request.query_params))
        except ValueError as exc:                      # state mismatch etc.
            log.warning("auth callback rejected: %s", exc)
            return RedirectResponse("/login?error=verify", status_code=303)
        if "error" in result:
            log.warning("auth error %s: %s", result.get("error"), result.get("error_description"))
            return RedirectResponse("/login?error=microsoft", status_code=303)
        claims = result.get("id_token_claims", {})
        email = claims_email(claims)
        allowed, reason = access_decision(cfg, claims)
        if not allowed:
            log.warning("access denied for oid %s: %s", claims.get("oid"), reason)
            request.session.clear()
            return HTMLResponse(denied_page(email or "This account", reason, logout_url(cfg)), status_code=403)
        log.info("access granted for oid %s via %s", claims.get("oid"), reason)
        request.session["user"] = {"name": claims.get("name") or email, "email": email, "oid": claims.get("oid"),
                                   "policy": cfg.access_policy}
        return RedirectResponse(safe_next(request.session.pop("next", "/")), status_code=303)

    @app.get("/logout", include_in_schema=False)
    def auth_logout(request: Request):
        request.session.clear()
        return RedirectResponse(logout_url(cfg) if cfg.configured else "/login", status_code=303)
