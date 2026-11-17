"""Private SQLite storage for AirSupply's derived nightly summaries.

No raw therapy logs, PINs or master pairing keys are persisted here.
Each successful complete history pull updates the affected nights atomically.
"""

import datetime as dt
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_PATH = "/data/airsupply-history.sqlite3"


class HistoryStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("AIRSUPPLY_HISTORY_DB", DEFAULT_PATH))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            self._initialise()
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            with db:
                yield db
        finally:
            db.close()

    def _initialise(self):
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS nightly (
                    address TEXT NOT NULL,
                    night TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    PRIMARY KEY(address, night)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS history_sync (
                    address TEXT PRIMARY KEY,
                    last_success TEXT NOT NULL
                )
            """)

    def save(self, address, nights, *, at=None):
        """Upsert completed summaries and record a successful sync in one transaction."""
        address = address.upper()
        at = at or dt.datetime.now(dt.timezone.utc)
        stamp = at.astimezone(dt.timezone.utc).isoformat()
        with self._connect() as db:
            for night in nights:
                db.execute("""
                    INSERT INTO nightly(address, night, summary) VALUES(?, ?, ?)
                    ON CONFLICT(address, night) DO UPDATE SET summary=excluded.summary
                """, (address, night["night"], json.dumps(night, sort_keys=True)))
            db.execute("""
                INSERT INTO history_sync(address, last_success) VALUES(?, ?)
                ON CONFLICT(address) DO UPDATE SET last_success=excluded.last_success
            """, (address, stamp))

    def recent(self, address, limit=14):
        """Most recent reporting nights, newest first."""
        with self._connect() as db:
            rows = db.execute("""
                SELECT summary FROM nightly WHERE address=?
                ORDER BY night DESC LIMIT ?
            """, (address.upper(), limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def last_success(self, address):
        with self._connect() as db:
            row = db.execute(
                "SELECT last_success FROM history_sync WHERE address=?",
                (address.upper(),),
            ).fetchone()
        return dt.datetime.fromisoformat(row[0]) if row else None

    def forget(self, address):
        """Erase history when the user removes a machine."""
        with self._connect() as db:
            db.execute("DELETE FROM nightly WHERE address=?", (address.upper(),))
            db.execute("DELETE FROM history_sync WHERE address=?", (address.upper(),))
