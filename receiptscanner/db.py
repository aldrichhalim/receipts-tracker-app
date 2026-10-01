"""SQLite storage for reviewed receipts."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,

    -- provenance: 'image', 'email_render' (e-receipt drawn from its markup),
    -- 'email_image' (photo attached to a message), 'email' (legacy rows, read
    -- as text before the render path existed)
    source_kind       TEXT DEFAULT 'image',
    email_message_id  TEXT,
    email_subject     TEXT,
    email_from        TEXT,
    email_date        TEXT,

    -- files
    source_path       TEXT,
    source_filename   TEXT,
    scanned_path      TEXT,
    archived_path     TEXT,

    -- ocr
    ocr_text          TEXT,
    ocr_text_raw      TEXT,
    ocr_lang          TEXT,
    ocr_confidence    REAL,
    ocr_word_count    INTEGER,

    -- reviewed entry
    entry_date        TEXT,
    category          TEXT,
    name              TEXT,
    amount            REAL,
    currency          TEXT,
    notes             TEXT,

    -- image details
    source_width      INTEGER,
    source_height     INTEGER,
    source_bytes      INTEGER,
    source_sha256     TEXT,
    scanned_width     INTEGER,
    scanned_height    INTEGER,
    exif_datetime     TEXT,
    camera_make       TEXT,
    camera_model      TEXT,
    document_detected INTEGER,
    deskew_angle      REAL,

    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipts_sha256 ON receipts(source_sha256);
CREATE INDEX IF NOT EXISTS idx_receipts_date   ON receipts(entry_date);
CREATE INDEX IF NOT EXISTS idx_receipts_cat    ON receipts(category);
CREATE INDEX IF NOT EXISTS idx_receipts_msgid  ON receipts(email_message_id);
"""

# Columns added after v1. CREATE TABLE IF NOT EXISTS leaves an existing table
# alone, so these are applied by hand to databases created before mail import.
V2_COLUMNS = (
    ("source_kind", "TEXT DEFAULT 'image'"),
    ("email_message_id", "TEXT"),
    ("email_subject", "TEXT"),
    ("email_from", "TEXT"),
    ("email_date", "TEXT"),
)

COLUMNS = (
    "source_kind",
    "email_message_id",
    "email_subject",
    "email_from",
    "email_date",
    "source_path",
    "source_filename",
    "scanned_path",
    "archived_path",
    "ocr_text",
    "ocr_text_raw",
    "ocr_lang",
    "ocr_confidence",
    "ocr_word_count",
    "entry_date",
    "category",
    "name",
    "amount",
    "currency",
    "notes",
    "source_width",
    "source_height",
    "source_bytes",
    "source_sha256",
    "scanned_width",
    "scanned_height",
    "exif_datetime",
    "camera_make",
    "camera_model",
    "document_detected",
    "deskew_angle",
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ReceiptStore:
    """Thin data layer. One connection, used only from the UI thread."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._connection:
            existed = bool(
                self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipts'"
                ).fetchone()
            )

            # Add missing columns first: SCHEMA creates an index over
            # email_message_id, which a pre-v2 table does not have yet.
            if existed:
                present = {
                    row["name"]
                    for row in self._connection.execute("PRAGMA table_info(receipts)")
                }
                for name, definition in V2_COLUMNS:
                    if name not in present:
                        self._connection.execute(
                            f"ALTER TABLE receipts ADD COLUMN {name} {definition}"
                        )

            self._connection.executescript(SCHEMA)

            current = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if current != SCHEMA_VERSION:
                self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:
            pass

    # -- writes ----------------------------------------------------------
    def save(self, record: dict[str, Any], record_id: int | None = None) -> int:
        payload = {key: record.get(key) for key in COLUMNS}
        payload["updated_at"] = _now()

        with self._connection:
            if record_id is not None:
                assignments = ", ".join(f"{key}=:{key}" for key in payload)
                self._connection.execute(
                    f"UPDATE receipts SET {assignments} WHERE id=:id",
                    {**payload, "id": record_id},
                )
                return record_id

            payload["created_at"] = _now()
            names = ", ".join(payload)
            placeholders = ", ".join(f":{key}" for key in payload)
            cursor = self._connection.execute(
                f"INSERT INTO receipts ({names}) VALUES ({placeholders})", payload
            )
            return int(cursor.lastrowid)

    def delete(self, record_id: int) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM receipts WHERE id=?", (record_id,))

    # -- reads -----------------------------------------------------------
    def find_by_hash(self, sha256: str) -> sqlite3.Row | None:
        if not sha256:
            return None
        return self._connection.execute(
            "SELECT * FROM receipts WHERE source_sha256=? ORDER BY id DESC LIMIT 1",
            (sha256,),
        ).fetchone()

    def find_by_message_id(self, message_id: str) -> sqlite3.Row | None:
        if not message_id:
            return None
        return self._connection.execute(
            "SELECT * FROM receipts WHERE email_message_id=? ORDER BY id DESC LIMIT 1",
            (message_id,),
        ).fetchone()

    def entries_between(self, start: str, end: str) -> list[sqlite3.Row]:
        """Saved entries with entry_date in [start, end], both inclusive.

        Dates are stored as ISO strings, so a plain BETWEEN sorts correctly.
        Rows with no date are left out: they cannot belong to a date range.
        """
        return list(
            self._connection.execute(
                "SELECT * FROM receipts "
                "WHERE entry_date IS NOT NULL AND entry_date != '' "
                "  AND entry_date BETWEEN ? AND ? "
                "ORDER BY entry_date, id",
                (start, end),
            ).fetchall()
        )

    def date_bounds(self) -> tuple[str | None, str | None]:
        """Earliest and latest entry_date on record."""
        row = self._connection.execute(
            "SELECT MIN(entry_date) AS lo, MAX(entry_date) AS hi FROM receipts "
            "WHERE entry_date IS NOT NULL AND entry_date != ''"
        ).fetchone()
        return (row["lo"], row["hi"]) if row else (None, None)

    def recent(self, limit: int = 200) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM receipts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    def summary(self) -> tuple[int, dict[str, float]]:
        """Row count, and the total per currency (rupiah and dollars never mix).

        Rows that never stored a currency are grouped under the empty string.
        """
        count = int(
            self._connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        )
        totals = {
            row["currency"]: float(row["total"])
            for row in self._connection.execute(
                "SELECT COALESCE(currency, '') AS currency, "
                "       COALESCE(SUM(amount), 0) AS total "
                "FROM receipts GROUP BY COALESCE(currency, '') ORDER BY 1"
            )
        }
        return count, totals
