"""
scripts/backup_db.py
====================
Online, crash-consistent SQLite database backup utility for KiranaIQ.
Uses Python sqlite3 Connection.backup() to create zero-downtime hot snapshots
while WAL writes and web workers continue uninterrupted.

Usage:
  # Backup to default ./backups directory, keeping latest 7 backups:
  python scripts/backup_db.py

  # Backup to custom directory with 14-day retention:
  python scripts/backup_db.py --dest-dir /var/backups/kirana_iq --keep 14
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.storage.sqlite_store import SQLiteStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backup_db")


def perform_backup(dest_dir: Path, db_path: Path | None = None, keep: int = 7) -> Path:
    """
    Perform a hot online backup of the primary SQLite database.
    Validates integrity of the backup file before finishing.
    """
    if db_path is None:
        db_path = Path(settings.sqlite_db_path)
    if not db_path.exists():
        logger.error("Source database file does not exist: %s", db_path)
        sys.exit(1)

    dest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = dest_dir / f"bills_backup_{timestamp}.db"

    logger.info("Starting online backup of %s -> %s", db_path, backup_file)

    store = SQLiteStore(str(db_path))
    try:
        store.backup(str(backup_file))
    finally:
        store.close()

    # Integrity verification
    logger.info("Validating backup file integrity: %s", backup_file)
    verify_conn = sqlite3.connect(str(backup_file))
    try:
        cur = verify_conn.execute("PRAGMA integrity_check;")
        row = cur.fetchone()
        result = row[0] if row else "unknown"
        if result != "ok":
            logger.error("Backup integrity check FAILED: %s", result)
            sys.exit(2)
        logger.info("Backup integrity check PASSED: ok")
    finally:
        verify_conn.close()

    size_bytes = backup_file.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    logger.info("Backup completed successfully: %s (%.2f MB, %d bytes)", backup_file.name, size_mb, size_bytes)

    # Retention pruning
    if keep > 0:
        backups = sorted(dest_dir.glob("bills_backup_*.db"))
        if len(backups) > keep:
            excess = len(backups) - keep
            logger.info("Pruning %d older backup(s) to maintain retention limit of %d", excess, keep)
            for old_backup in backups[:excess]:
                try:
                    old_backup.unlink()
                    logger.info("Deleted pruned backup: %s", old_backup.name)
                except Exception as exc:
                    logger.warning("Failed to delete %s: %s", old_backup, exc)

    return backup_file


def main() -> None:
    parser = argparse.ArgumentParser(description="KiranaIQ Database Online Backup Utility")
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to source SQLite database file (default: settings.sqlite_db_path)",
    )
    parser.add_argument(
        "--dest-dir",
        default=str(PROJECT_ROOT / "backups"),
        help="Destination directory for database backups (default: ./backups)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=7,
        help="Number of recent backups to retain (default: 7, 0 to disable pruning)",
    )
    args = parser.parse_args()

    db_p = Path(args.db_path) if args.db_path else None
    perform_backup(dest_dir=Path(args.dest_dir), db_path=db_p, keep=args.keep)


if __name__ == "__main__":
    main()
