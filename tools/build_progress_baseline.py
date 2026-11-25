#!/usr/bin/env python3
"""Build the Progress v1 baseline and calibration artifacts from local snapshots.

The AA Progress v1 data package is gone (404). This script freezes the last
official 5.0.1 Progress snapshot, marks the 28 live /trends anchors, and fits a
small quadratic calibration from the 5.0.1 AA Index to the 5.0.1 Progress score.
The runtime uses the baseline for known models and the calibration only for
models that appeared after the snapshot.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetch_model_data as F
import mr_paths

OLD_PAGE_TS = "2026-11-24 20:37:54"
OLD_PROGRESS_TS = "2026-11-24 20:38:09"
TRENDS_URL = "https://artificialanalysis.ai/trends"


def page_models(con: sqlite3.Connection, ts: str) -> list[dict]:
    row = con.execute(
        "select payload from snapshots where source='aa_html' and ts=?", (ts,)
    ).fetchone()
    if not row:
        raise SystemExit(f"aa_html snapshot not found: {ts}")
    html = json.loads(row[0])[0]["html"]
    text = F._rsc_text(html)
    match = re.search(r'"initialModels":', text)
    if not match:
        raise SystemExit("initialModels not found")
    return json.JSONDecoder().raw_decode(text[match.end():].lstrip())[0]


def progress_row_key(row: dict) -> tuple:
    release = row.get("release")
    slug = release.get("slug") if isinstance(release, dict) else row.get("release_slug")
    _, tier = F.split_series(str(row.get("name") or ""))
    return (slug, F._tier_key(tier))


def model_key(model: dict) -> tuple:
    release = model.get("release")
    slug = release.get("slug") if isinstance(release, dict) else model.get("slug")
    _, tier = F.split_series(str(model.get("name") or ""))
    return (slug, F._tier_key(tier))


def trends_anchor_keys() -> set[tuple]:
    try:
        html = F.fetch(TRENDS_URL, timeout=30)
        if isinstance(html, bytes):
            html = html.decode("utf-8", "ignore")
        text = F._rsc_text(html)
        match = re.search(r'"initialModels":', text)
        if not match:
            return set()
        rows = json.JSONDecoder().raw_decode(text[match.end():].lstrip())[0]
        return {model_key(row) for row in rows}
    except Exception as exc:  # network is optional for artifact rebuilds
        print(f"warning: /trends anchors unavailable: {exc}", file=sys.stderr)
        return set()


def quad_fit(xs: list[float], ys: list[float]) -> list[float]:
    n = len(xs)
    sx = sum(xs)
    sx2 = sum(x * x for x in xs)
    sx3 = sum(x ** 3 for x in xs)
    sx4 = sum(x ** 4 for x in xs)
    sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sx2y = sum(x * x * y for x, y in zip(xs, ys))
    a = [[n, sx, sx2], [sx, sx2, sx3], [sx2, sx3, sx4]]
    b = [sy, sxy, sx2y]
    for i in range(3):
        pivot = max(range(i, 3), key=lambda r: abs(a[r][i]))
        a[i], a[pivot] = a[pivot], a[i]
        b[i], b[pivot] = b[pivot], b[i]
        for r in range(i + 1, 3):
            factor = a[r][i] / a[i][i]
            for c in range(i, 3):
                a[r][c] -= factor * a[i][c]
            b[r] -= factor * b[i]
    out = [0.0, 0.0, 0.0]
    for i in range(2, -1, -1):
        out[i] = (b[i] - sum(a[i][j] * out[j] for j in range(i + 1, 3))) / a[i][i]
    return out


def quad_predict(coefficients: list[float], x: float) -> float:
    return coefficients[0] + coefficients[1] * x + coefficients[2] * x * x


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(mr_paths.DB_PATH))
    parser.add_argument("--out-dir", default=str(ROOT))
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select payload from snapshots where source='aa_progress' and ok=1 and ts=?",
        (OLD_PROGRESS_TS,),
    ).fetchone()
    if not rows:
        raise SystemExit(f"aa_progress snapshot not found: {OLD_PROGRESS_TS}")
    progress = json.loads(rows[0])
    page = page_models(con, OLD_PAGE_TS)
    page_by_key = {model_key(model): model for model in page}

    anchors = trends_anchor_keys()
    baseline = []
    for row in progress:
        score = row.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        key = progress_row_key(row)
        model = page_by_key.get(key) or {}
        baseline.append({
            "name": row.get("name"),
            "release": key[0],
            "tier": key[1],
            "score": round(float(score), 6),
            "intelligenceIndex": model.get("intelligenceIndex"),
            "anchor": key in anchors,
        })

    pairs = []
    for row in baseline:
        index = row.get("intelligenceIndex")
        if isinstance(index, bool) or not isinstance(index, (int, float)):
            continue
        pairs.append((float(index), float(row["score"])))
    if len(pairs) < 20:
        raise SystemExit("not enough paired rows for calibration")
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    coefficients = quad_fit(xs, ys)
    residuals = [quad_predict(coefficients, x) - y for x, y in pairs]
    mae = sum(abs(r) for r in residuals) / len(residuals)
    mean_y = sum(ys) / len(ys)
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum(r * r for r in residuals)

    model = {
        "version": "v1",
        "trained_on": OLD_PAGE_TS,
        "target_snapshot": OLD_PROGRESS_TS,
        "features": ["intelligenceIndex"],
        "model": "quadratic",
        "coefficients": [round(c, 10) for c in coefficients],
        "n": len(pairs),
        "metrics": {
            "mae": round(mae, 4),
            "r2": round(1 - ss_res / ss_tot, 4),
        },
        "anchors": len(anchors),
        "note": "Progress v1 IRT is not reproduced; this is a monotone calibration.",
    }
    baseline_doc = {
        "version": "v1",
        "snapshot": OLD_PROGRESS_TS,
        "official": True,
        "source": "aa_progress 5.0.1",
        "scores": baseline,
    }

    out = Path(args.out_dir)
    (out / "progress_v1_model.json").write_text(
        json.dumps(model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "progress_v1_baseline.json").write_text(
        json.dumps(baseline_doc, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"baseline={len(baseline)} paired={len(pairs)} anchors={len(anchors)} "
          f"mae={mae:.2f} r2={1 - ss_res / ss_tot:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
