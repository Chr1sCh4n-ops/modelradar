#!/usr/bin/env python3
"""Restate the derived text in the published payload with the current wording.

`latest.json` stores the text fields that the crawler computed at that round:
`moves[].text`, `move_lines`, `metric_views`, `prev_moves`, `cumulative` and
`alert`. A wording change in the code does not reach them until the next crawl,
so the chart keeps showing the old sentences.

This tool rebuilds them from the ledger round the payload points at, through
`tools/round_text.py`. It refuses to write unless the rebuilt payload matches
the stored one after every derived text field is removed: a numeric or
structural difference means the replay is not faithful, and publishing a mixed
record is worse than publishing nothing.

Read-only by default. Pass --apply to write latest.json, latest.js and the
browser mirror.

Usage:
    python3 tools/restate_payload_text.py            # show what changes
    python3 tools/restate_payload_text.py --apply    # write it
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

TEXT_KEYS = ("relation", "text", "lines", "items", "move_lines", "hint",
             "lines_plain", "metric_lines")
PAYLOAD_KEYS = ("move_lines", "moves", "prev_moves", "cumulative",
                "metric_views", "changes", "alert", "last_alert")
# 文本派生块：它们里面的 `n` 是"行数"的复述，行数一变它必然跟着变，不该算成数值漂移。
# 别处的 `n`（如 `sources.aa.n` 的模型数）是真实观测，必须留在护栏里。
DERIVED_BLOCKS = {"last_alert", "prev_moves", "cumulative", "alert", "moves",
                  "move_lines", "metric_views"}


def _strip(node, derived: bool = False):
    """Drop every derived text field, keeping numbers and structure."""
    if isinstance(node, dict):
        drop = set(TEXT_KEYS) | ({"n"} if derived else set())
        return {k: _strip(v, derived or k in DERIVED_BLOCKS)
                for k, v in node.items() if k not in drop}
    if isinstance(node, list):
        return [_strip(v, derived) for v in node]
    return node


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the payload back")
    ap.add_argument("--path", default=str(mr_paths.LATEST_JSON), help="payload path")
    args = ap.parse_args()

    path = Path(args.path)
    try:
        D = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"读不到 {path}：{exc}", file=sys.stderr)
        return 2
    cfg = F.load_config()
    con = sqlite3.connect(mr_paths.DB_PATH)
    try:
        view = round_view(con, str(D.get("diff_ts") or ""), cfg)
        if view is None:
            print("台账里找不到这一轮，不改。", file=sys.stderr)
            return 3
        new = json.loads(json.dumps(D))      # deep copy; the record is small
        # 关联读数按 (模型, 字段) 从重放结果搬回来：changes 的数字与结构原样保留
        relations = {(c["name"], f["field"]): f.get("relation")
                     for c in view["changes"] for f in c.get("fields") or []}
        for c in new.get("changes") or []:
            for f in c.get("fields") or []:
                rel = relations.get((c.get("name"), f.get("field")))
                if rel:
                    f["relation"] = rel
        new["moves"] = view["moves"]
        new["move_lines"] = view["move_lines"]
        new["prev_moves"] = view["prev_moves"]
        new["cumulative"] = view["cumulative"]
        new["metric_views"] = view["metric_views"]
        new["alert"] = F.alert_policy(new)
        new["alert"]["hint"] = F.move_hint()
        for metric, mv in new["metric_views"].items():
            mv["alert"] = F.alert_policy({**new, **mv})
            mv["alert"]["hint"] = F.move_hint(metric)
        # 「上次提示」是**更早**那一轮的记录，按它自己的轮次重建，不能套本轮的 changes。
        la = new.get("last_alert") or {}
        if la.get("ts") or la.get("at"):
            prior = round_view(con, str(la.get("ts") or la.get("at") or ""), cfg)
            if prior:
                new["last_alert"] = rewrite_alert(la, prior)
    finally:
        con.close()

    # 护栏：去掉所有派生文本后必须逐字相同。不同说明重放不忠实，宁可停下也不发布半新半旧的一份。
    if _strip(new) != _strip(D):
        print("重放出的载荷与已发布的那份在非文本字段上不一致，停下不改。", file=sys.stderr)
        print("请先核对台账与 latest.json 是否来自同一轮。", file=sys.stderr)
        return 4

    changed = [k for k in PAYLOAD_KEYS if new.get(k) != D.get(k)]
    if not changed:
        print("文案已经是最新的，不需要改。")
        return 0
    print(f"轮次 {D.get('diff_ts')}：更新 {', '.join(changed)}")
    for line in new["move_lines"]:
        print("  · " + line)
    if not args.apply:
        print("\n(dry run) 加 --apply 写回。")
        return 0
    F.write_outputs(new)
    print(f"\n已写回 {path} 并镜像给看板")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
