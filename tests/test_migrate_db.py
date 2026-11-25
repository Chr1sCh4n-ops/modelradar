#!/usr/bin/env python3
"""第六批 Radar：合成数据库验证 WAL、冲突、失败、重跑，不运行安装器。"""
from contextlib import closing
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "widget" / "migrate_db.py"
spec = importlib.util.spec_from_file_location("migrate_db", SCRIPT)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def make_db(path, value):
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE history(value TEXT)")
        con.execute("INSERT INTO history VALUES(?)", (value,))
        con.commit()


def rows(path):
    with closing(sqlite3.connect(path)) as con:
        return con.execute("SELECT * FROM history").fetchall()


class TestMigration(unittest.TestCase):
    def test_uncheckpointed_wal_snapshot_and_rerun(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "legacy.db", Path(td) / "data" / "ModelRadar.db"
            with closing(sqlite3.connect(source)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE history(value TEXT)")
                writer.execute("INSERT INTO history VALUES('only in WAL')")
                writer.commit()
                wal = Path(str(source) + "-wal")
                self.assertGreater(wal.stat().st_size, 0)
                before = source.read_bytes(), wal.read_bytes()
                self.assertTrue(M.migrate_db(target, [source]))
                self.assertEqual(rows(target), [("only in WAL",)])
                self.assertEqual((source.read_bytes(), wal.read_bytes()), before)
                with closing(sqlite3.connect(target)) as con:
                    self.assertEqual(con.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                snapshot = target.read_bytes()
                self.assertFalse(M.migrate_db(target, [source]))
                self.assertEqual(target.read_bytes(), snapshot)
                self.assertEqual((source.read_bytes(), wal.read_bytes()), before)
                self.assertEqual(list(target.parent.glob(".modelradar-backup-*")), [])

    def test_completed_migration_accepts_destination_growth_but_rejects_source_change(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "legacy.db", Path(td) / "target.db"
            with closing(sqlite3.connect(source)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE history(value TEXT)")
                writer.execute("INSERT INTO history VALUES('original')")
                writer.commit()
                self.assertTrue(M.migrate_db(target, [source]))
                with closing(sqlite3.connect(target)) as con:
                    con.execute("INSERT INTO history VALUES('new destination history')")
                    con.commit()
                self.assertFalse(M.migrate_db(target, [source]))
                before = target.read_bytes()
                writer.execute("INSERT INTO history VALUES('new source WAL history')")
                writer.commit()
                with self.assertRaises(ValueError):
                    M.migrate_db(target, [source])
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(rows(source), [("original",), ("new source WAL history",)])
                self.assertEqual(rows(target), [("original",), ("new destination history",)])

    def test_identical_independent_databases_require_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "legacy.db", Path(td) / "target.db"
            make_db(source, "same")
            target.write_bytes(source.read_bytes())
            with self.assertRaises(ValueError):
                M.migrate_db(target, [source])
            self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_replaced_source_and_invalid_receipt_refuse(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "legacy.db", Path(td) / "target.db"
            make_db(source, "same")
            self.assertTrue(M.migrate_db(target, [source]))
            replacement = Path(td) / "replacement.db"
            replacement.write_bytes(source.read_bytes())
            os.replace(replacement, source)
            with self.assertRaises(ValueError):
                M.migrate_db(target, [source])
            with closing(sqlite3.connect(target)) as con:
                con.execute("DELETE FROM modelradar_migration_receipt")
                con.commit()
            with self.assertRaises(ValueError):
                M.migrate_db(target, [source])
            self.assertEqual(rows(target), [("same",)])

    def test_multiple_sources_or_existing_destination_refuse(self):
        with tempfile.TemporaryDirectory() as td:
            a, b, target = [Path(td) / n for n in ("a.db", "b.db", "target.db")]
            make_db(a, "first")
            make_db(b, "second" * 20000)
            for existing in (False, True):
                if existing:
                    make_db(target, "target")
                before = {p: p.read_bytes() for p in (a, b, target) if p.exists()}
                with self.assertRaises(ValueError):
                    M.migrate_db(target, [a, b])
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                self.assertEqual(target.exists(), existing)

    def test_same_file_alias_and_missing_source_are_noops(self):
        with tempfile.TemporaryDirectory() as td:
            target, alias = Path(td) / "target.db", Path(td) / "alias.db"
            self.assertFalse(M.migrate_db(target, [alias]))
            make_db(target, "same")
            alias.symlink_to(target)
            self.assertFalse(M.migrate_db(target, [alias, target]))
            self.assertEqual(rows(target), [("same",)])

    def test_corrupt_source_keeps_source_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "bad.db", Path(td) / "target.db"
            source.write_bytes(b"not a SQLite database")
            with self.assertRaises(sqlite3.Error):
                M.migrate_db(target, [source])
            self.assertEqual(source.read_bytes(), b"not a SQLite database")
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(td).glob(".modelradar-backup-*")), [])

    def test_publish_failure_keeps_source_and_can_retry(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "a.db", Path(td) / "target.db"
            make_db(source, "retained")
            before = source.read_bytes()
            with patch.object(M.os, "link", side_effect=OSError("synthetic failure")):
                with self.assertRaises(OSError):
                    M.migrate_db(target, [source])
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(td).glob(".modelradar-backup-*")), [])
            self.assertTrue(M.migrate_db(target, [source]))
            self.assertEqual(rows(target), [("retained",)])

    def test_integrity_failure_never_publishes(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "a.db", Path(td) / "target.db"
            make_db(source, "retained")
            connect = sqlite3.connect
            class BadSnapshot:
                def __init__(self, *args, **kw):
                    self.con = connect(*args, **kw)
                def backup(self, destination):
                    self.con.backup(destination.con)
                def execute(self, sql):
                    if sql == "PRAGMA integrity_check":
                        class Result:
                            def fetchall(self):
                                return [("synthetic integrity failure",)]
                        return Result()
                    return self.con.execute(sql)
                def close(self):
                    self.con.close()
            with patch.object(M.sqlite3, "connect", BadSnapshot):
                with self.assertRaisesRegex(ValueError, "integrity"):
                    M.migrate_db(target, [source])
            self.assertFalse(target.exists())
            self.assertEqual(rows(source), [("retained",)])

    def test_concurrent_destination_never_gets_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "a.db", Path(td) / "target.db"
            make_db(source, "source")
            link = os.link
            def raced_link(src, dst):
                make_db(target, "concurrent")
                return link(src, dst)
            with patch.object(M.os, "link", side_effect=raced_link):
                with self.assertRaises(FileExistsError):
                    M.migrate_db(target, [source])
            self.assertEqual(rows(target), [("concurrent",)])
            self.assertEqual(rows(source), [("source",)])

    def test_cli_completed_migration_reruns_without_copy(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "a.db", Path(td) / "target.db"
            make_db(source, "source")
            before = source.read_bytes()
            command = [sys.executable, "-I", str(SCRIPT), str(target), str(source)]
            first = subprocess.run(command, cwd=td, env={**os.environ, "HOME": td},
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(first.returncode, 0, first.stderr)
            snapshot = target.read_bytes()
            again = subprocess.run(command, cwd=td, env={**os.environ, "HOME": td},
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("needs no copy", again.stdout)
            self.assertEqual(target.read_bytes(), snapshot)
            self.assertEqual(source.read_bytes(), before)

    def test_cli_conflict_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "a.db", Path(td) / "target.db"
            make_db(source, "source")
            make_db(target, "target")
            result = subprocess.run([sys.executable, "-I", str(SCRIPT), str(target), str(source)],
                                    cwd=td, env={**os.environ, "HOME": td},
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Multiple databases", result.stderr)
            self.assertEqual(rows(source), [("source",)])
            self.assertEqual(rows(target), [("target",)])


if __name__ == "__main__":
    unittest.main()
