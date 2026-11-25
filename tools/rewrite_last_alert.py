#!/usr/bin/env python3
"""Rewrite the frozen `.last_alert.json` text with the current wording.

`.last_alert.json` keeps the lines that were pushed at one round. The crawler
writes it once and never touches it again, so a wording change leaves that
record in the old shape until the next alert fires. Every other history block
is recomputed from the ledger at render time and already follows the code.

This tool rebuilds the record from the ledger round it names, through
`tools/round_text.py` (the same path `restate_payload_text.py` uses). It
changes only text: `at`, `ts`, `kind`, `label`, `tone` and `key` stay as they
are. Read-only by default. Pass --apply to write.

Usage:
    python3 tools/rewrite_last_alert.py            # show the diff
    python3 tools/rewrite_last_alert.py --apply    # write the file
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetch_model_data as F
import mr_paths
from tools.round_text import rewrite_alert, round_view


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the record back")
    ap.add_argument("--path", default=str(mr_paths.LAST_ALERT), help="record path")
    args = ap.parse_args()

    path = Path(args.path)
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"读不到 {path}：{exc}", file=sys.stderr)
        return 2
    ts = str(rec.get("ts") or rec.get("at") or "")
    con = sqlite3.connect(mr_paths.DB_PATH)
    try:
        view = round_view(con, ts, F.load_config())
    finally:
        con.close()
    if view is None:
        print(f"台账里找不到 {ts} 这一轮或它的基线，不改。", file=sys.stderr)
        return 3

    new = rewrite_alert(rec, view)
    old_lines = [str(x) for x in (rec.get("lines") or [])]
    if (old_lines == new["lines"] and str(rec.get("text") or "") == new["text"]
            and (rec.get("metric_lines") or {}) == new["metric_lines"]):
        print("文案已经是最新的，不需要改。")
        return 0
    print(f"轮次 {ts}（{rec.get('kind')}）：{len(old_lines)} 行 → {len(new['lines'])} 行")
    for line in new["lines"]:
        print("  · " + line)
    if not args.apply:
        print("\n(dry run) 加 --apply 写回。")
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    print(f"\n已写回 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
