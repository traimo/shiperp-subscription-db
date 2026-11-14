"""Accepted exceptions: an issue that is correct as it is, accepted with a reason.

Stored in data/checks/accepted.json (outside the database, so it survives re-imports), keyed by the issue key
(rule + workbook + tab + cell). The value fingerprint is stored too: when the cell value changes, the acceptance no
longer applies and the issue re-opens ("reopened")."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

FILE = "accepted.json"


def _path(out_dir: Path) -> Path:
    return Path(out_dir) / FILE


def load(out_dir: Path) -> dict:
    p = _path(out_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save(out_dir: Path, data: dict) -> None:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    tmp = _path(out_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(_path(out_dir))


def apply(issues, out_dir: Path) -> None:
    acc = load(out_dir)
    for i in issues:
        a = acc.get(i.key)
        if not a:
            i.status, i.accepted = "open", None
        elif a.get("fingerprint") == i.fingerprint:
            i.status, i.accepted = "accepted", {k: a.get(k) for k in ("reason", "by", "at")}
        else:
            i.status, i.accepted = "reopened", {k: a.get(k) for k in ("reason", "by", "at")}


def accept(out_dir: Path, issue, reason: str, by: str) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Give a reason for accepting this issue.")
    acc = load(out_dir)
    acc[issue.key] = {"fingerprint": issue.fingerprint, "reason": reason[:300], "by": by,
                      "at": dt.datetime.now().isoformat(timespec="seconds"), "rule": issue.rule, "ref": issue.ref,
                      "current": issue.current, "message": issue.message}
    _save(out_dir, acc)
    issue.status, issue.accepted = "accepted", {"reason": reason[:300], "by": by, "at": acc[issue.key]["at"]}


def reopen(out_dir: Path, issue) -> None:
    acc = load(out_dir)
    acc.pop(issue.key, None)
    _save(out_dir, acc)
    issue.status, issue.accepted = "open", None
