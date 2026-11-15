"""Fill the Key Vault that az-deploy-agent created for this app with the values the hosted app needs.

    python scripts/azure_secrets.py --vault kv-shiperp-subscr-xxxx [--dry-run]

Run it after the deployer's first pass has created the (empty) vault and given you Key Vault Secrets Officer, then run
the deployer again. Every `${NAME}` without a default in compose.yaml becomes Key Vault secret `name-with-dashes`.
Where each value comes from:
  - SP_*                 this project's .env (the read-only SharePoint app for Refresh data)
  - ENTRA_*              shiperp-dashboard's .env: sign-in reuses the dashboard's Entra app (same access group)
  - DATABASE_URL         built from AZ_PG_SUBS_PASSWORD in .env: login `subs` on psql-shiperp-subs, TLS required
  - SESSION_SECRET_KEY   generated fresh (never a local key)
  - REDIRECT_URI         https://<app FQDN>/auth/callback (--fqdn; add it to the Entra app's web redirect URIs)
Values go to `az keyvault secret set` through a temporary file, so they never appear on the console or in the process
list. --dry-run lists the secret names and what is missing, and sets nothing.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_ENV = ROOT.parent / "shiperp-dashboard" / ".env"
DEFAULT_FQDN = "shiperp-subscription-db-app.bravesmoke-02934eef.westus2.azurecontainerapps.io"
PG_HOST = "psql-shiperp-subs.postgres.database.azure.com"

FROM_ENV = ["SP_TENANT_ID", "SP_CLIENT_ID", "SP_CLIENT_SECRET", "SP_SUBSCRIPTION_URL", "SP_MAINTENANCE_URL"]
FROM_DASHBOARD = ["ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"]
DERIVED = ["DATABASE_URL", "SESSION_SECRET_KEY", "REDIRECT_URI"]
ALL = FROM_ENV + FROM_DASHBOARD + DERIVED


def kv_name(env_name: str) -> str:
    return env_name.lower().replace("_", "-")


def read_env(path: Path) -> dict[str, str]:
    """KEY=value lines of a .env file (comments and blank values skipped)."""
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
            if m and m.group(2).strip().strip('"'):
                out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def compose_secret_names(compose: Path = ROOT / "compose.yaml") -> set[str]:
    """`${NAME}` references without a default in compose.yaml: the secrets the deployer will look up."""
    code = "\n".join(line.split("#", 1)[0] for line in compose.read_text(encoding="utf-8").splitlines())
    return set(re.findall(r"\$\{([A-Z0-9_]+)\}", code))


def collect(env: dict[str, str], dash: dict[str, str], fqdn: str) -> tuple[dict[str, str], list[str]]:
    values = {n: env[n] for n in FROM_ENV if env.get(n)}
    values.update({n: dash[n] for n in FROM_DASHBOARD if dash.get(n)})
    if env.get("AZ_PG_SUBS_PASSWORD"):
        values["DATABASE_URL"] = (f"postgresql+psycopg://subs:{env['AZ_PG_SUBS_PASSWORD']}@{PG_HOST}:5432/"
                                  "subscriptions?sslmode=require")
    values["SESSION_SECRET_KEY"] = secrets.token_hex(32)
    values["REDIRECT_URI"] = f"https://{fqdn}/auth/callback"
    missing = [n for n in ALL if n not in values]
    return values, missing


def set_secret(vault: str, name: str, value: str) -> bool:
    fd, path = tempfile.mkstemp(prefix="kv-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(value)
        r = subprocess.run(["az", "keyvault", "secret", "set", "--vault-name", vault, "--name", name,
                            "--file", path, "--encoding", "utf-8", "--only-show-errors", "-o", "none"],
                           capture_output=True, text=True, shell=(os.name == "nt"))
        if r.returncode != 0:
            print(f"  FAIL {name}: {r.stderr.strip()[:300]}")
            return False
        return True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", help="Key Vault name from the deployer's first-pass report (kv-shiperp-subscr-xxxx)")
    ap.add_argument("--fqdn", default=DEFAULT_FQDN, help="the app's host name (from the deployer's report)")
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    ap.add_argument("--dashboard-env", default=str(DASHBOARD_ENV), help="shiperp-dashboard's .env (ENTRA_* values)")
    ap.add_argument("--dry-run", action="store_true", help="list secret names and what is missing; set nothing")
    args = ap.parse_args(argv)

    unexpected = compose_secret_names() ^ set(ALL)
    if unexpected:
        print(f"compose.yaml and this script disagree on the secrets: {', '.join(sorted(unexpected))}")
        return 2
    values, missing = collect(read_env(Path(args.env_file)), read_env(Path(args.dashboard_env)), args.fqdn)

    print(f"Secrets for vault {args.vault or '<vault>'} (values are never printed):")
    for name in ALL:
        src = ("shiperp-dashboard .env" if name in FROM_DASHBOARD else "generated" if name == "SESSION_SECRET_KEY"
               else "--fqdn" if name == "REDIRECT_URI" else "AZ_PG_SUBS_PASSWORD" if name == "DATABASE_URL"
               else ".env")
        print(f"  {kv_name(name):22s} <- {name} ({src}){'  MISSING' if name in missing else ''}")
    if args.dry_run:
        return 1 if missing else 0
    if not args.vault:
        print("--vault is required (take the name from the deployer's first-pass report)")
        return 2
    if missing:
        print(f"Missing: {', '.join(missing)}. Add them first.")
        return 1

    found = subprocess.run(["az", "keyvault", "show", "--name", args.vault, "--query", "name", "-o", "tsv",
                            "--only-show-errors"], capture_output=True, text=True, shell=(os.name == "nt"))
    if found.returncode != 0:
        print(f"Key Vault {args.vault} not found. Run the deployer's first pass and approve 'prepare app resource "
              "group' first: it creates the vault and gives you Key Vault Secrets Officer on it.")
        return 2

    ok = True
    for name in ALL:
        good = set_secret(args.vault, kv_name(name), values[name])
        print(f"  {'OK  ' if good else 'FAIL'} {kv_name(name)}")
        ok &= good
    print("\nDone." if ok else "\nSome secrets were not set; fix and rerun.")
    print(f"Add {values['REDIRECT_URI']} to the Entra app's web redirect URIs (the dashboard's app registration).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
