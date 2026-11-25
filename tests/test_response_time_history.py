"""Backfill ttft_s / total_s into series from raw AA snapshots."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import backfill_response_times as B


SCHEMA = """
CREATE TABLE snapshots(
    ts TEXT NOT NULL, source TEXT NOT NULL, n_rows INTEGER, ok INTEGER,
    err TEXT, payload TEXT, evaluation_version TEXT, PRIMARY KEY (ts, source));
CREATE TABLE series(
    ts TEXT NOT NULL, name TEXT NOT NULL, field TEXT NOT NULL, value REAL,
    evaluation_version TEXT, PRIMARY KEY (ts, name, field));
"""


class ResponseTimeHistory(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db = Path(self.td.name) / "history.db"
        self.con = sqlite3.connect(self.db)
        self.con.executescript(SCHEMA)

    def tearDown(self):
        self.con.close()
        self.td.cleanup()

    def add_snapshot(self, ts, rows, version=None, source="aa", ok=1):
        self.con.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)",
            (ts, source, len(rows), ok, None, json.dumps(rows), version))
        self.con.commit()

    def series(self):
        return self.con.execute(
            "SELECT ts,name,field,value,evaluation_version FROM series "
            "ORDER BY ts,name,field").fetchall()

    def test_preserves_snapshot_ts_and_evaluation_version(self):
        self.add_snapshot(
            "2026-10-01 10:00:00",
            [{"name": "Row Versioned", "ttft_s": 1.5, "total_s": 10,
              "evaluation_version": "4.3.2"},
             {"name": "Snapshot Versioned", "ttft_s": 2.5, "total_s": 11}],
            version="5.0")
        self.add_snapshot(
            "2026-10-02 10:00:00",
            [{"name": "No Version", "ttft_s": 3, "total_s": 12}],
            version=None)

        stats = B.backfill(self.con, apply=True)

        self.assertEqual(stats["written"], 6)
        self.assertEqual(self.series(), [
            ("2026-10-01 10:00:00", "Row Versioned", "total_s", 10.0, "4.3.2"),
            ("2026-10-01 10:00:00", "Row Versioned", "ttft_s", 1.5, "4.3.2"),
            ("2026-10-01 10:00:00", "Snapshot Versioned", "total_s", 11.0, "5.0"),
            ("2026-10-01 10:00:00", "Snapshot Versioned", "ttft_s", 2.5, "5.0"),
            ("2026-10-02 10:00:00", "No Version", "total_s", 12.0, None),
            ("2026-10-02 10:00:00", "No Version", "ttft_s", 3.0, None),
        ])

    def test_idempotent_and_does_not_overwrite_existing(self):
        self.add_snapshot("2026-10-01 10:00:00",
                          [{"name": "A", "ttft_s": 1.0, "total_s": 5}])
        self.con.execute(
            "INSERT INTO series VALUES('2026-10-01 10:00:00','A','ttft_s',99,'old')")
        self.con.commit()

        first = B.backfill(self.con, apply=True)
        second = B.backfill(self.con, apply=True)

        self.assertEqual(first["written"], 1)
        self.assertEqual(first["already_present"], 1)
        self.assertEqual(second["written"], 0)
        self.assertEqual(self.con.execute(
            "SELECT value,evaluation_version FROM series "
            "WHERE ts='2026-10-01 10:00:00' AND name='A' AND field='ttft_s'"
        ).fetchone(), (99.0, "old"))

    def test_raw_snapshot_is_unchanged(self):
        rows = [{"name": "A", "ttft_s": 1.0, "total_s": 5}]
        self.add_snapshot("2026-10-01 10:00:00", rows, version="5.0")
        before = self.con.execute("SELECT * FROM snapshots").fetchall()

        B.backfill(self.con, apply=True)

        after = self.con.execute("SELECT * FROM snapshots").fetchall()
        self.assertEqual(after, before)
        self.assertEqual(json.loads(after[0][5]), rows)

    def test_invalid_values_are_skipped(self):
        payload = [
            {"name": "Reject", "ttft_s": -1, "total_s": True},
            {"name": "Reject", "ttft_s": float("nan"), "total_s": float("inf")},
            {"name": "Reject", "ttft_s": "1.2", "total_s": None},
            {"name": "", "ttft_s": 1.0, "total_s": 2.0},
            {"name": "Keep", "ttft_s": 0, "total_s": 4.0},
        ]
        self.add_snapshot("2026-10-01 10:00:00", payload)

        stats = B.backfill(self.con, apply=True)

        self.assertEqual(stats["skipped_invalid"], 6)
        self.assertEqual(stats["skipped_name"], 1)
        self.assertEqual(self.series(), [
            ("2026-10-01 10:00:00", "Keep", "total_s", 4.0, None),
            ("2026-10-01 10:00:00", "Keep", "ttft_s", 0.0, None),
        ])
        fields = {row[2] for row in self.series()}
        self.assertNotIn("time_per_task", fields)

    def test_ignores_non_aa_and_failed_snapshots(self):
        rows = [{"name": "A", "ttft_s": 1.0, "total_s": 5}]
        self.add_snapshot("2026-10-01 10:00:00", rows, source="diffbase")
        self.add_snapshot("2026-10-01 10:00:01", rows, source="aa", ok=0)

        stats = B.backfill(self.con, apply=True)

        self.assertEqual(stats["snapshots"], 0)
        self.assertEqual(self.series(), [])

    def test_dry_run_writes_nothing(self):
        self.add_snapshot("2026-10-01 10:00:00",
                          [{"name": "A", "ttft_s": 1.0, "total_s": 5}])

        stats = B.backfill(self.con, apply=False)

        self.assertEqual(stats["written"], 2)
        self.assertEqual(self.series(), [])


if __name__ == "__main__":
    unittest.main()
