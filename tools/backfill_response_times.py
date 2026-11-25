#!/usr/bin/env python3
"""Backfill series ttft_s / total_s from successful raw AA snapshots.

Read-only by default. Pass --apply to write. Existing observations are never
overwritten (INSERT OR IGNORE). Raw snapshot rows are never modified.

Only rows from source='aa' snapshots with ok=1 are used. Carried/stale merged
rows (diffbase snapshots) are excluded by construction: they live under a
different snapshot source.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mr_paths

SOURCE = "aa"
FIELDS = ("ttft_s", "total_s")


def _connect(db_path: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _valid_value(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value >= 0


def _row_version(row: dict, snapshot_version: object) -> object:
    version = row.get("evaluation_version")
    if isinstance(version, str) and version.strip():
        return version
    return snapshot_version if isinstance(snapshot_version, str) else None


def backfill(con: sqlite3.Connection, apply: bool = False) -> dict:
    """Backfill one AA snapshot series value per row/field.

    Returns counters for reporting. With apply=False the connection is only
    read and no transaction writes happen.
    """
    snapshot_columns = _columns(con, "snapshots")
    if not snapshot_columns:
        raise ValueError("snapshots table is missing")
    series_columns = _columns(con, "series")
    if not series_columns:
        raise ValueError("series table is missing")
    if not {"ts", "name", "field", "value"} <= series_columns:
        raise ValueError("series table lacks required columns")
    if "evaluation_version" not in series_columns:
        raise ValueError("series.evaluation_version is missing; run ensure_db first")

    version_expr = ("evaluation_version" if "evaluation_version" in snapshot_columns
                    else "NULL")
    rows = con.execute(
        "SELECT ts, payload, " + version_expr + " FROM snapshots "
        "WHERE source=? AND ok=1 AND payload IS NOT NULL ORDER BY ts",
        (SOURCE,),
    ).fetchall()

    stats = {
        "snapshots": 0,
        "raw_rows": 0,
        "valid": 0,
        "already_present": 0,
        "written": 0,
        "bad_payload": 0,
        "skipped_invalid": 0,
        "skipped_name": 0,
    }

    candidates: list[tuple[str, str, str, float, str | None]] = []
    for ts, payload, snapshot_version in rows:
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            stats["bad_payload"] += 1
            continue
        if not isinstance(parsed, list):
            stats["bad_payload"] += 1
            continue
        stats["snapshots"] += 1
        for row in parsed:
            if not isinstance(row, dict):
                stats["skipped_invalid"] += 1
                continue
            stats["raw_rows"] += 1
            name = row.get("name")
            if not isinstance(name, str) or not name.strip():
                stats["skipped_name"] += 1
                continue
            version = _row_version(row, snapshot_version)
            for field in FIELDS:
                value = row.get(field)
                if not _valid_value(value):
                    stats["skipped_invalid"] += 1
                    continue
                stats["valid"] += 1
                candidates.append((ts, name, field, float(value), version))

    def exists(item: tuple[str, str, str, float, str | None]) -> bool:
        ts, name, field = item[0], item[1], item[2]
        return con.execute(
            "SELECT 1 FROM series WHERE ts=? AND name=? AND field=?",
            (ts, name, field),
        ).fetchone() is not None

    if apply:
        con.execute("BEGIN IMMEDIATE")
        try:
            for item in candidates:
                if exists(item):
                    stats["already_present"] += 1
                    continue
                cur = con.execute(
                    "INSERT OR IGNORE INTO series"
                    "(ts,name,field,value,evaluation_version) VALUES(?,?,?,?,?)",
                    item,
                )
                if cur.rowcount:
                    stats["written"] += 1
                else:
                    stats["already_present"] += 1
            con.commit()
        except BaseException:
            con.rollback()
            raise
    else:
        for item in candidates:
            if exists(item):
                stats["already_present"] += 1
            else:
                stats["written"] += 1
    return stats


def _report(db_path: Path, stats: dict, apply: bool) -> None:
    print(f"db: {db_path}")
    print(f"mode: {'apply' if apply else 'dry-run (pass --apply to write)'}")
    print(f"aa snapshots: {stats['snapshots']}")
    print(f"raw rows: {stats['raw_rows']}")
    print(f"valid observations: {stats['valid']}")
    print(f"already present: {stats['already_present']}")
    print(f"{'inserted' if apply else 'would insert'}: {stats['written']}")
    print(f"skipped invalid value: {stats['skipped_invalid']}")
    print(f"skipped missing name: {stats['skipped_name']}")
    print(f"skipped bad payload: {stats['bad_payload']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill series ttft_s/total_s from successful raw AA snapshots.")
    parser.add_argument("--db", type=Path, default=mr_paths.DB_PATH)
    parser.add_argument("--apply", action="store_true",
                        help="Write rows. Default is a read-only report.")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    con = _connect(args.db, readonly=not args.apply)
    try:
        stats = backfill(con, apply=args.apply)
    finally:
        con.close()
    _report(args.db, stats, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
