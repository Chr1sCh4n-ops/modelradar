#!/usr/bin/env python3
"""Rebuild one ledger round's derived text with the current wording.

Two frozen records keep text written by the crawler at one round:
`.last_alert.json` (that round's notification) and `latest.json` (the text
fields of the published payload). The code that wrote them has since changed
wording, and neither file updates until a new round arrives. This module holds
the one rebuild path both tools use, so the two records cannot drift apart.

`round_view` replays the snapshot pair of a round through the same helpers
`run()` uses. It never invents values: keys, numbers and structure come from
the ledger, only the derived strings are recomputed.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetch_model_data as F

METRICS = ("tok_s", "time_per_task_s")


def round_changes(con: sqlite3.Connection, ts: str, cfg: dict):
    """Changes of the round at `ts`, rebuilt from its snapshot pair.

    Returns None when the round or its baseline is missing from the ledger.
    """
    rows = con.execute("SELECT ts, payload FROM snapshots WHERE source=? AND ok=1 "
                       "AND payload IS NOT NULL ORDER BY ts DESC", (F.SRC_DIFF,)).fetchall()
    order = [str(r[0]) for r in rows]
    if ts not in order:
        return None
    i = order.index(ts)
    if i + 1 >= len(rows):
        return None
    cur = [{**value, "name": name} for name, value in
           F.baseline_map(json.loads(rows[i][1]), cfg, ts=str(rows[i][0])).items()]
    prev = F.baseline_map(json.loads(rows[i + 1][1]), cfg, ts=str(rows[i + 1][0]))
    return F.diff_models(cur, prev)


def round_view(con: sqlite3.Connection, ts: str, cfg: dict) -> dict | None:
    """Every derived text field of one round, as `run()` would build it now.

    `cumulative` uses the round's own rows as "current", so the text answers the
    same question the original record did. `metric_views` mirrors the payload
    shape, including each view's own alert and hint.
    """
    rows = con.execute("SELECT ts, payload FROM snapshots WHERE source=? AND ok=1 "
                       "AND payload IS NOT NULL ORDER BY ts DESC", (F.SRC_DIFF,)).fetchall()
    order = [str(r[0]) for r in rows]
    if ts not in order:
        return None
    i = order.index(ts)
    changes = round_changes(con, ts, cfg)
    if changes is None:
        return None
    models = [{**value, "name": name} for name, value in
              F.baseline_map(json.loads(rows[i][1]), cfg, ts=str(rows[i][0])).items()]
    moves = F.notable_moves(changes)
    cumulative = F.cumulative_moves(con, models, cfg)
    keys = set(F.move_keys(moves)) | set(cumulative.get("keys") or [])
    views = {}
    for metric in METRICS:
        selected = F.notable_moves(changes, metric)
        cum = F.cumulative_moves(con, models, cfg, metric=metric)
        mkeys = set(F.move_keys(selected)) | set(cum.get("keys") or [])
        views[metric] = {
            "moves": selected,
            "move_lines": F.merged_move_lines(selected, changes),
            "cumulative": cum,
            "last_moves": F.last_notable_round(con, selected, cfg, skip_keys=mkeys, metric=metric),
            "prev_moves": F.last_notable_round(con, [], cfg, skip_keys=mkeys, metric=metric),
        }
    return {"ts": ts, "changes": changes, "models": models, "moves": moves,
            "move_lines": F.merged_move_lines(moves, changes),
            "prev_moves": F.last_notable_round(con, [], cfg, skip_keys=keys),
            "cumulative": cumulative, "metric_views": views}


def alert_lines(view: dict, kind: str) -> list[str]:
    """The lines that notification carried, chosen by its kind.

    A `moved` alert lists that round's own moves. A `moved-cumulative` alert
    fires in a quiet round, so its lines are the cumulative block's; using
    `move_lines` there would produce an empty list and erase the record.
    """
    if kind == "moved-cumulative":
        return [str(x) for x in (view["cumulative"].get("lines") or []) if x]
    return [str(x) for x in (view["move_lines"] or
                             [m["text"] for m in view["moves"]]) if x]


def alert_text(view: dict, kind: str) -> str:
    """The banner text of that alert, in the same shape `alert_policy` builds."""
    if kind == "moved-cumulative":
        return F.cumulative_text(view["cumulative"])
    if kind == "moved":
        return F.moves_text(view["moves"])
    return ""


def rewrite_alert(rec: dict, view: dict) -> dict:
    """Same shape as `alert_record`, rebuilt from `view`."""
    kind = str(rec.get("kind") or "")
    lines = alert_lines(view, kind)
    text = alert_text(view, kind) or str(rec.get("text") or "")
    metric_lines = {}
    for metric in METRICS:
        v = view["metric_views"].get(metric) or {}
        metric_lines[metric] = ([str(x) for x in (v.get("move_lines") or []) if x]
                                if kind == "moved"
                                else [str(x) for x in (v.get("cumulative", {}).get("lines")
                                                       or []) if x])
    return {**rec, "text": text, "n": len(lines), "lines": lines,
            "metric_lines": metric_lines}
