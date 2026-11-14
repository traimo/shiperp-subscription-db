"""Read-only Microsoft Graph client: download the two source workbooks from SharePoint for "Refresh data".

Only reads (GET). It never writes to SharePoint; contract documents stay links-only (the app never opens them).
Auth: client credentials of the Entra app in .env (SP_TENANT_ID, SP_CLIENT_ID, SP_CLIENT_SECRET) with
Sites.Selected *read* granted on the site that holds the workbooks. The files are named by their SharePoint
sharing links: SP_SUBSCRIPTION_URL and SP_MAINTENANCE_URL.
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import time
from dataclasses import dataclass
from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"
SOURCES = {"subscription": "SP_SUBSCRIPTION_URL", "maintenance": "SP_MAINTENANCE_URL"}
_CRED_KEYS = ("SP_TENANT_ID", "SP_CLIENT_ID", "SP_CLIENT_SECRET")


class SharePointError(RuntimeError):
    pass


@dataclass
class RemoteFile:
    key: str                      # "subscription" | "maintenance"
    name: str
    size: int
    modified: dt.datetime | None
    modified_by: str
    drive_id: str
    item_id: str
    web_url: str = ""


def encode_share_url(url: str) -> str:
    """Sharing link -> Graph share id (u! + base64url without padding)."""
    return "u!" + base64.urlsafe_b64encode(url.strip().encode("utf-8")).decode("ascii").rstrip("=")


def share_urls() -> dict[str, str]:
    return {k: os.environ.get(env, "").strip() for k, env in SOURCES.items() if os.environ.get(env, "").strip()}


def missing_settings() -> list[str]:
    miss = [k for k in _CRED_KEYS if not os.environ.get(k, "").strip()]
    return miss + [env for k, env in SOURCES.items() if not os.environ.get(env, "").strip()]


def configured() -> bool:
    return not missing_settings()


def _parse_time(v: str | None) -> dt.datetime | None:
    if not v:
        return None
    try:   # Graph returns UTC "2026-11-13T21:04:45Z" -> local naive time for display / comparison
        return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


class GraphClient:
    def __init__(self, session=None):
        import requests
        self.http = session or requests.Session()
        self._token: str | None = None
        self._expiry = 0.0

    def token(self) -> str:
        if self._token and time.monotonic() < self._expiry - 300:
            return self._token
        miss = [k for k in _CRED_KEYS if not os.environ.get(k, "").strip()]
        if miss:
            raise SharePointError("SharePoint access is not set up: " + ", ".join(miss) + " missing in .env.")
        tenant = os.environ["SP_TENANT_ID"].strip()
        r = self.http.post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", timeout=30,
                           data={"grant_type": "client_credentials", "client_id": os.environ["SP_CLIENT_ID"].strip(),
                                 "client_secret": os.environ["SP_CLIENT_SECRET"].strip(),
                                 "scope": "https://graph.microsoft.com/.default"})
        body = _json(r)
        if r.status_code >= 400 or "access_token" not in body:
            # error code + first line of the description only (never echo request data)
            desc = (str(body.get("error_description", "")).splitlines() or [""])[0][:200] if body else r.text[:200]
            raise SharePointError(f"Sign-in to Microsoft Graph failed ({body.get('error', r.status_code)}): {desc}")
        self._token = body["access_token"]
        self._expiry = time.monotonic() + float(body.get("expires_in", 3600))
        return self._token

    def _get(self, path: str, **kw):
        r = self.http.get(path if path.startswith("http") else GRAPH + path,
                          headers={"Authorization": f"Bearer {self.token()}"}, timeout=kw.pop("timeout", 60), **kw)
        if r.status_code in (401, 403):
            raise SharePointError(f"The app has no access to this SharePoint file ({r.status_code}). Grant the Entra "
                                  "app Sites.Selected read on the site that holds the workbooks.")
        if r.status_code == 404:
            raise SharePointError("SharePoint file not found (404) - check the sharing link in .env.")
        if r.status_code >= 400:
            raise SharePointError(f"Graph request failed ({r.status_code}): {r.text[:200]}")
        return r

    def resolve(self, key: str, share_url: str) -> RemoteFile:
        d = _json(self._get(f"/shares/{encode_share_url(share_url)}/driveItem"))
        by = ((d.get("lastModifiedBy") or {}).get("user") or {}).get("displayName", "")
        return RemoteFile(key=key, name=d.get("name", ""), size=int(d.get("size") or 0),
                          modified=_parse_time(d.get("lastModifiedDateTime")), modified_by=by,
                          drive_id=(d.get("parentReference") or {}).get("driveId", ""), item_id=d.get("id", ""),
                          web_url=d.get("webUrl", ""))

    def download(self, f: RemoteFile, dest: Path) -> Path:
        r = self._get(f"/drives/{f.drive_id}/items/{f.item_id}/content", stream=True, timeout=300)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
        return dest


def _json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


def resolve_all(client: GraphClient | None = None) -> dict[str, RemoteFile]:
    """Metadata of both workbooks (raises SharePointError when not configured or not reachable)."""
    miss = missing_settings()
    if miss:
        raise SharePointError("SharePoint pull is not set up: " + ", ".join(miss) + " missing in .env.")
    client = client or GraphClient()
    return {k: client.resolve(k, url) for k, url in share_urls().items()}
