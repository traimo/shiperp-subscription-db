"""python -m app.checker [--no-import] [--export]  - run the spreadsheet check and print a summary."""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from . import CHECK_DIR, run_check
from . import export


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Check the source workbooks for mistakes before importing.")
    ap.add_argument("--no-import", action="store_true", help="sheet rules only (skip the trial import)")
    ap.add_argument("--export", action="store_true", help="also write the issue list and highlighted copies")
    args = ap.parse_args(argv)
    res = run_check(with_import=not args.no_import)
    print(f"Checked {res.ran_at}: {len(res.issues)} issues")
    for sev, n in res.counts().items():
        print(f"  {sev:9} open {n['open']:4}  accepted {n['accepted']:4}")
    print("By rule (open):")
    for (rule, title), n in Counter((i.rule, i.title) for i in res.issues if i.status != "accepted").most_common():
        print(f"  {rule:4} {n:4}  {title}")
    if args.export:
        print(export.issue_list(res, CHECK_DIR / "spreadsheet-issues.xlsx"))
        for w in ("subscription", "maintenance"):
            if res.sources.get(w):
                print(export.annotated_copy(res, w, CHECK_DIR))
    print(f"Details: {CHECK_DIR / 'latest-check.json'}  (or the Spreadsheet check page)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
