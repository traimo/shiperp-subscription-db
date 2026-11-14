"""Set up / check SharePoint access for "Refresh data".

    python scripts/sharepoint_access.py                 # check only (changes nothing)
    python scripts/sharepoint_access.py --write-links   # also rewrite SP_SUBSCRIPTION_URL / SP_MAINTENANCE_URL in .env by workbook name
    python scripts/sharepoint_access.py --apply         # also grant the app read on the fi_team site if it is missing

1. App check (no sign-in): with SP_TENANT_ID / SP_CLIENT_ID / SP_CLIENT_SECRET from .env, gets an app token and opens
   both sharing links. Prints only file names, modified time and which workbook each link is.
2. Only when the app cannot read the files (or with --apply): signs YOU in interactively (browser, Microsoft Graph
   PowerShell public client, scope Sites.FullControl.All - needs a SharePoint / Global admin), finds the fi_team site
   and checks its app permissions. The read grant (Sites.Selected) is added only with --apply, and only when missing.

Never prints or writes the client secret. The app already used by shiperp-dashboard ("ShipERP Financial Dashboard")
was granted read on fi_team by that project's scripts/sharepoint_setup.py, so normally step 2 is not needed.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config, sharepoint  # noqa: E402  (loads .env)

GRAPH = "https://graph.microsoft.com/v1.0"
HOST = "erpintegratedsolutions.sharepoint.com"
SITE = "fi_team"
PS_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"     # Microsoft Graph PowerShell (public client)
# The two workbooks' sharing links come from .env (SP_SUBSCRIPTION_URL / SP_MAINTENANCE_URL); never hard-code them.
LINKS = [u for u in (os.environ.get("SP_SUBSCRIPTION_URL", "").strip(), os.environ.get("SP_MAINTENANCE_URL", "").strip()) if u]
PATTERNS = {"subscription": "*Subscription Summary*.xlsx", "maintenance": "*Maintenance*.xlsx"}
ENV_KEYS = {"subscription": "SP_SUBSCRIPTION_URL", "maintenance": "SP_MAINTENANCE_URL"}


def app_check() -> tuple[str, dict[str, str]]:
    """('ok' | 'no-access' | 'no-signin', {workbook key: link})."""
    miss = [k for k in ("SP_TENANT_ID", "SP_CLIENT_ID", "SP_CLIENT_SECRET", "SP_SUBSCRIPTION_URL", "SP_MAINTENANCE_URL")
            if not os.environ.get(k, "").strip()]
    if miss:
        print("app check skipped:", ", ".join(miss), "missing in .env")
        return "no-signin", {}
    client = sharepoint.GraphClient()
    try:
        client.token()
        print("app sign-in: OK")
    except sharepoint.SharePointError as e:
        print("app sign-in FAILED:", e)
        return "no-signin", {}
    found, ok = {}, True
    for url in LINKS:
        try:
            f = client.resolve("?", url)
        except sharepoint.SharePointError as e:
            print(f"  {url[-24:]}: {e}")
            ok = False
            continue
        key = next((k for k, pat in PATTERNS.items() if Path(f.name).match(pat)), None)
        when = f.modified.strftime("%m/%d/%Y %H:%M") if f.modified else "?"
        print(f"  {url[-24:]}: {f.name}  (modified {when} by {f.modified_by or '?'})"
              f"  -> {key or 'NOT a source workbook'}")
        if key:
            found[key] = url
        else:
            ok = False
    return ("ok" if ok and len(found) == 2 else "no-access"), found


def write_links(found: dict[str, str]) -> None:
    p = ROOT / ".env"
    text = p.read_text(encoding="utf-8")
    for key, url in found.items():
        k = ENV_KEYS[key]
        if re.search(rf"^{k}=.*$", text, flags=re.M):
            text = re.sub(rf"^{k}=.*$", lambda _: f"{k}={url}", text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n{k}={url}\n"
    p.write_text(text, encoding="utf-8")
    print("wrote", ", ".join(ENV_KEYS[k] for k in found), "to .env")


def admin_token() -> str:
    import msal
    tenant = os.environ["SP_TENANT_ID"].strip()
    app = msal.PublicClientApplication(PS_CLIENT_ID, authority=f"https://login.microsoftonline.com/{tenant}")
    res = app.acquire_token_interactive(scopes=["Sites.FullControl.All"], prompt="select_account")
    if "access_token" not in res:
        sys.exit(f"sign-in failed: {res.get('error_description', res)}")
    print("signed in as", res.get("id_token_claims", {}).get("preferred_username", "?"))
    return res["access_token"]


def grant_check(apply: bool) -> None:
    app_id = os.environ.get("SP_CLIENT_ID", "").strip()
    if not app_id:
        sys.exit("SP_CLIENT_ID missing in .env")
    h = {"Authorization": f"Bearer {admin_token()}"}
    r = requests.get(f"{GRAPH}/sites/{HOST}:/sites/{SITE}", headers=h, timeout=60)
    if r.status_code >= 400:
        sys.exit(f"site {SITE}: {r.status_code} {r.text[:200]}")
    site = r.json()
    r = requests.get(f"{GRAPH}/sites/{site['id']}/permissions", headers=h, timeout=60)
    if r.status_code >= 400:
        sys.exit(f"permissions: {r.status_code} {r.text[:200]} (needs a SharePoint / Global admin)")
    for p in r.json().get("value", []):
        for ident in p.get("grantedToIdentitiesV2", p.get("grantedToIdentities", [])):
            app = ident.get("application") or {}
            if app.get("id") == app_id:
                print(f"grant already present on {SITE}: {p.get('roles')} for {app.get('displayName', app_id)} "
                      "- nothing to change in Azure")
                return
    if not apply:
        print(f"MISSING: app {app_id[:8]}... has no permission on {SITE}. Re-run with --apply to grant 'read'.")
        return
    r = requests.post(f"{GRAPH}/sites/{site['id']}/permissions", headers={**h, "Content-Type": "application/json"},
                      json={"roles": ["read"], "grantedToIdentities": [{"application": {"id": app_id}}]}, timeout=60)
    if r.status_code >= 400:
        sys.exit(f"grant failed: {r.status_code} {r.text[:200]}")
    print(f"granted 'read' on {SITE} to app {app_id[:8]}... (Sites.Selected)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write-links", action="store_true", help="write the two sharing links to .env")
    ap.add_argument("--apply", action="store_true", help="grant the app read on fi_team if it is missing")
    a = ap.parse_args()
    status, found = app_check()
    if found and a.write_links:
        write_links(found)
    if status == "ok" and not a.apply:
        print("OK - the app can read both workbooks; nothing to change in Azure.")
        return
    if status == "no-signin" and not a.apply:
        sys.exit("Fix the app credentials in .env first (SP_TENANT_ID / SP_CLIENT_ID / SP_CLIENT_SECRET - the secret "
                 "VALUE, not the Secret ID); a site grant cannot help while the app cannot sign in.")
    if status == "no-access":
        print("The app signs in but cannot read both workbooks - checking the site permission (admin sign-in) ...")
    grant_check(a.apply)
    if a.apply:
        print("Re-run without --apply to confirm the app can read both workbooks.")


if __name__ == "__main__":
    main()
