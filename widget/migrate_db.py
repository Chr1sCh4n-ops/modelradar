#!/usr/bin/env python3
"""Copy one legacy database with SQLite backup. Keep every source file."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from contextlib import closing


def migrate_db(destination: Path, sources: list[Path]) -> bool:
    """Reject distinct databases before copying. Return True after a new snapshot."""
    candidates = []
    for path in [destination, *sources]:
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError(f"Database path is not a file: {path}")
        if not any(os.path.samefile(path, other) for other in candidates):
            candidates.append(path)
    conflict = "Multiple databases exist. Keep the sources and select one database manually."
    receipt = None
    if len(candidates) > 1:
        if len(candidates) != 2 or not destination.exists():
            raise ValueError(conflict)
        source = candidates[1]
        try:
            with closing(sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)) as dst:
                receipts = dst.execute("SELECT source, device, inode, sha256 "
                                       "FROM modelradar_migration_receipt").fetchall()
        except sqlite3.Error as exc:
            raise ValueError(conflict) from exc
        identity = (str(source.resolve()), source.stat().st_dev, source.stat().st_ino)
        if len(receipts) != 1 or receipts[0][:3] != identity:
            raise ValueError(conflict)
        receipt = receipts[0]
    elif not candidates or destination.exists():
        return False
    else:
        source = candidates[0]
    if receipt is None:
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(str(destination) + suffix).exists():
                raise ValueError("Destination sidecar exists. Keep the sources and examine the destination.")
    identity = (str(source.resolve()), source.stat().st_dev, source.stat().st_ino)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".modelradar-backup-", suffix=".db", dir=destination.parent)
    os.close(fd)
    snapshot = Path(name)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
                result = dst.execute("PRAGMA integrity_check").fetchall()
                if result != [("ok",)]:
                    raise ValueError("Snapshot integrity failed. Keep the source database.")
                dst.execute("PRAGMA journal_mode=DELETE")
        digest = hashlib.sha256()
        with snapshot.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        fingerprint = digest.hexdigest()
        if identity != (str(source.resolve()), source.stat().st_dev, source.stat().st_ino):
            raise ValueError("The source database changed during migration. Keep both databases.")
        if receipt is not None:
            if fingerprint != receipt[3]:
                raise ValueError(conflict)
            return False
        # Plan batch 6: the receipt binds the retained source to this completed migration.
        # The source identity rejects independent copies. The hash includes committed WAL data.
        with closing(sqlite3.connect(snapshot)) as dst:
            dst.execute("CREATE TABLE modelradar_migration_receipt("
                        "source TEXT, device INTEGER, inode INTEGER, sha256 TEXT)")
            dst.execute("INSERT INTO modelradar_migration_receipt VALUES(?,?,?,?)",
                        (*identity, fingerprint))
            dst.commit()
        # The link creates the destination only if no other process created it.
        os.link(snapshot, destination)
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(snapshot) + suffix).unlink(missing_ok=True)
    return True


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("Usage: migrate_db.py DESTINATION SOURCE [SOURCE ...]")
    try:
        copied = migrate_db(Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]])
    except (OSError, sqlite3.Error, ValueError) as exc:
        sys.exit(f"Database migration failed: {exc}")
    print("Database snapshot created. Source retained." if copied else "Database migration needs no copy.")


if __name__ == "__main__":
    main()
