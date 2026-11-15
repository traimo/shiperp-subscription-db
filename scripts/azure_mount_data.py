"""Mount the Azure Files share `subs-data` on /app/data of the hosted app (manual fallback), or --check that it is.

    python scripts/azure_mount_data.py [--check]

compose.yaml declares the share under `x-azure.azure_files`; an az-deploy-agent that supports that mounts it on every
deploy and this script is only needed for --check. An older deployer sets the app's volumes to [] on each deploy:
then run this right after the deploy. Without the share the app still runs (with a red banner on every page), but
/app/data is the container's own disk: spreadsheet-check acceptances, reports and the pulled workbooks are lost on
the next restart. The mount must match compose.yaml (tests/test_data_mount.py checks it).

The share is registered once on the environment (`az containerapp env storage set ... --storage-name subs-data`).
This script patches only the app's template (volumes + the container's volumeMounts), which makes a new revision.
--check only reports whether the mount is there (exit 0 = mounted, 1 = not mounted).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile

RG = "rg-app-shiperp-subscription-db"
APP = "shiperp-subscription-db-app"
STORAGE = "subs-data"          # environment storage name on cae-agents-private
MOUNT = "/app/data"
UID = 10001                    # the image's `app` user (services/app/Dockerfile)


def az(*args: str) -> str:
    r = subprocess.run(["az", *args, "--only-show-errors"], capture_output=True, text=True, shell=(os.name == "nt"))
    if r.returncode != 0:
        raise SystemExit(f"az {' '.join(args[:3])} failed: {r.stderr.strip()[:500]}")
    return r.stdout


def is_mounted(template: dict) -> bool:
    vols = {v.get("name"): v for v in template.get("volumes") or []}
    for c in template.get("containers") or []:
        for m in c.get("volumeMounts") or []:
            v = vols.get(m.get("volumeName"))
            if m.get("mountPath") == MOUNT and v and v.get("storageType") == "AzureFile" and v.get("storageName") == STORAGE:
                return True
    return False


def with_mount(template: dict) -> dict:
    """The template with the share mounted on /app/data in every container (other volumes and mounts kept)."""
    t = copy.deepcopy(template)
    vols = [v for v in t.get("volumes") or [] if v.get("name") != STORAGE]
    vols.append({"name": STORAGE, "storageType": "AzureFile", "storageName": STORAGE,
                 "mountOptions": f"uid={UID},gid={UID},dir_mode=0750,file_mode=0640"})
    t["volumes"] = vols
    for c in t.get("containers") or []:
        mounts = [m for m in c.get("volumeMounts") or [] if m.get("mountPath") != MOUNT]
        mounts.append({"volumeName": STORAGE, "mountPath": MOUNT})
        c["volumeMounts"] = mounts
    return t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="only report whether the share is mounted")
    ap.add_argument("-g", "--resource-group", default=RG)
    ap.add_argument("-n", "--name", default=APP)
    args = ap.parse_args(argv)

    template = json.loads(az("containerapp", "show", "-g", args.resource_group, "-n", args.name,
                             "--query", "properties.template", "-o", "json"))
    if is_mounted(template):
        print(f"{STORAGE} is mounted on {MOUNT}.")
        return 0
    if args.check:
        print(f"NOT mounted: {MOUNT} is the container's own disk. Run this script without --check.")
        return 1
    patch = {"properties": {"template": {"containers": with_mount(template)["containers"],
                                         "volumes": with_mount(template)["volumes"]}}}
    fd, path = tempfile.mkstemp(prefix="aca-mount-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(patch, fh)            # JSON is valid YAML
        az("containerapp", "update", "-g", args.resource_group, "-n", args.name, "--yaml", path, "-o", "none")
    finally:
        os.remove(path)
    print(f"Mounted {STORAGE} on {MOUNT} (new revision). Check: python scripts/azure_mount_data.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
