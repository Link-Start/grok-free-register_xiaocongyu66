import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import JobStatus


class Ledger:
    def __init__(self, path: Path, salt: bytes):
        self.path = Path(path)
        self.salt = bytes(salt)
        self._init()

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id INTEGER PRIMARY KEY,
                    source_fingerprint TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    reason_code TEXT,
                    sink_receipt_fingerprint TEXT
                )
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
            }
            if "authorization_started" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN authorization_started INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_source_status_idx "
                "ON jobs(source_fingerprint, status)"
            )
        os.chmod(self.path, 0o600)

    def fingerprint(self, source_id):
        return hmac.new(self.salt, source_id.encode(), hashlib.sha256).hexdigest()

    def _fingerprint(self, source_id):
        return self.fingerprint(source_id)

    def start(self, source_id, attempt=1):
        return self.start_fingerprint(self.fingerprint(source_id), attempt=attempt)

    def start_fingerprint(self, source_fingerprint, attempt=None):
        if attempt is None:
            attempt = self.next_attempt(source_fingerprint)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO jobs(source_fingerprint, attempt_number, status, started_at) "
                "VALUES (?, ?, ?, ?)",
                (source_fingerprint, attempt, "pending", now),
            )
            return cursor.lastrowid

    def next_attempt(self, source_fingerprint):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next_attempt "
                "FROM jobs WHERE source_fingerprint=?",
                (source_fingerprint,),
            ).fetchone()
        return int(row["next_attempt"])

    def mark_authorization_started(self, job_id):
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET authorization_started=1 "
                "WHERE job_id=? AND status='pending'",
                (job_id,),
            )

    def finish(self, job_id, status, reason_code, sink_receipt_fingerprint=None):
        status_value = status.value if isinstance(status, JobStatus) else str(status)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, finished_at=?, reason_code=?, "
                "sink_receipt_fingerprint=? "
                "WHERE job_id=? AND status='pending'",
                (status_value, now, reason_code, sink_receipt_fingerprint, job_id),
            )

    def recover_pending(self, *, reason="recovered_pending"):
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, finished_at=?, reason_code=? WHERE status='pending'",
                (JobStatus.CANCELLED.value, datetime.now(timezone.utc).isoformat(), reason),
            )

    def has_imported(self, source_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM jobs WHERE source_fingerprint=? AND status=? LIMIT 1",
                (self.fingerprint(source_id), JobStatus.IMPORTED.value),
            ).fetchone()
        return row is not None

    def imported_fingerprints(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT source_fingerprint FROM jobs WHERE status=?",
                (JobStatus.IMPORTED.value,),
            )
            return {row["source_fingerprint"] for row in rows}

    def aggregate_counts(self):
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(DISTINCT CASE WHEN authorization_started=1 OR status=?
                        THEN source_fingerprint END) AS attempted_unique,
                    COUNT(DISTINCT CASE WHEN status=?
                        THEN source_fingerprint END) AS imported_unique,
                    COUNT(CASE WHEN finished_at IS NOT NULL THEN 1 END) AS finalized_attempts,
                    COUNT(CASE WHEN status=? THEN 1 END) AS imported_attempts,
                    COUNT(CASE WHEN reason_code='rate_limited' THEN 1 END) AS rate_limited
                FROM jobs
                """,
                (
                    JobStatus.IMPORTED.value,
                    JobStatus.IMPORTED.value,
                    JobStatus.IMPORTED.value,
                ),
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def get(self, job_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source_fingerprint, attempt_number, status, started_at, finished_at, "
                "reason_code, sink_receipt_fingerprint FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            return dict(row) if row else None
