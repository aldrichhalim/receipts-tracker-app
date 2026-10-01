"""SQLite storage for reviewed receipts."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Mapping, NamedTuple

SCHEMA_VERSION = 3

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

    -- derived by dedupe_key(); never supplied by a caller. NULL opts a row out
    -- of de-duplication. Its UNIQUE index is created by _migrate, not here: an
    -- older table must be backfilled and de-duplicated before it can exist.
    dedupe_key        TEXT,

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

# Added in v3. Not in COLUMNS: it is derived inside save(), so a caller's record
# can never carry a stale key into the table.
V3_COLUMNS = (("dedupe_key", "TEXT"),)

DEDUPE_INDEX = "idx_receipts_dedupe"

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


class SaveResult(NamedTuple):
    id: int
    created: bool  # False when an existing row was updated instead


def _cents(amount: Any) -> int | None:
    """Whole minor units, so REAL-versus-REAL equality can never split a match.

    Goes through the float's shortest repr (0.1 + 0.2 is "0.30000000000000004",
    not an exact binary expansion), then rounds half up.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return int((value * 100).to_integral_value(ROUND_HALF_UP))


def _normalize_title(name: Any) -> str:
    if not name:
        return ""
    folded = unicodedata.normalize("NFKC", str(name)).casefold()
    return " ".join(folded.split())


def dedupe_key(record: Mapping[str, Any]) -> str | None:
    """What makes two entries the same purchase: title, amount, currency, date.

    Titles compare case-, width- and whitespace-insensitively; amounts compare as
    whole cents. Returns None, opting the entry out of de-duplication, when the
    title, amount or date is missing: two untitled receipts that share an amount
    and a day are not evidence of one purchase.
    """
    title = _normalize_title(record.get("name"))
    cents = _cents(record.get("amount"))
    entry_date = str(record.get("entry_date") or "").strip()
    if not title or cents is None or not entry_date:
        return None
    currency = str(record.get("currency") or "").strip().upper()
    # JSON, not a joined string: a title containing "|" cannot forge a collision.
    return json.dumps(
        [title, cents, currency, entry_date], ensure_ascii=False, separators=(",", ":")
    )


class ReceiptStore:
    """Thin data layer. One connection, used only from the UI thread."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit: transactions are explicit (see _transaction), because
        # executescript() would otherwise commit one out from under a migration.
        self._connection = sqlite3.connect(str(self.path), isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        try:
            self._migrate()
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _apply_schema(self) -> None:
        """Run SCHEMA one statement at a time, inside the caller's transaction."""
        buffer = ""
        for line in SCHEMA.splitlines(keepends=True):
            buffer += line
            if sqlite3.complete_statement(buffer):
                self._connection.execute(buffer)
                buffer = ""

    def _row_count(self) -> int:
        return int(
            self._connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        )

    def _backup_before_migration(self) -> Path:
        """A consistent copy beside the database, taken before anything changes."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        counter = 1
        while target.exists():
            target = self.path.with_name(f"{self.path.name}.bak-{stamp}-{counter}")
            counter += 1

        destination = sqlite3.connect(str(target))
        try:
            self._connection.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
        finally:
            destination.close()
        # The copy inherits WAL mode, and SQLite leaves its -shm (and sometimes an
        # empty -wal) behind. With no connection open they are safe to remove.
        for suffix in ("-shm", "-wal"):
            sidecar = Path(f"{target}{suffix}")
            if sidecar.exists() and (suffix == "-shm" or sidecar.stat().st_size == 0):
                sidecar.unlink()
        return target

    def _collapse_duplicates(self) -> None:
        """Backfill every row's key and fold exact duplicates into the oldest.

        The survivor keeps its own fields, provenance and created_at; it only
        borrows notes from a discarded duplicate when it has none of its own.
        """
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in self._connection.execute(
            "SELECT id, name, amount, currency, entry_date, notes "
            "FROM receipts ORDER BY id"
        ).fetchall():
            key = dedupe_key(dict(row))
            if key is not None:
                groups.setdefault(key, []).append(row)

        for key, members in groups.items():
            survivor, duplicates = members[0], members[1:]
            notes = survivor["notes"]
            if not (notes or "").strip():
                notes = next(
                    (d["notes"] for d in duplicates if (d["notes"] or "").strip()),
                    notes,
                )
            for duplicate in duplicates:
                self._connection.execute(
                    "DELETE FROM receipts WHERE id=?", (duplicate["id"],)
                )
            self._connection.execute(
                "UPDATE receipts SET dedupe_key=?, notes=? WHERE id=?",
                (key, notes, survivor["id"]),
            )

    def _migrate(self) -> None:
        connection = self._connection
        existed = bool(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipts'"
            ).fetchone()
        )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        upgrading = existed and version < SCHEMA_VERSION

        # Before anything changes. A database with no rows has nothing to lose.
        if upgrading and self._row_count():
            self._backup_before_migration()

        # One transaction: a failure anywhere, including the ALTERs, rolls back.
        with self._transaction():
            if existed:
                # Columns first: SCHEMA indexes email_message_id, which a pre-v2
                # table does not have yet.
                present = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(receipts)")
                }
                for name, definition in (*V2_COLUMNS, *V3_COLUMNS):
                    if name not in present:
                        connection.execute(
                            f"ALTER TABLE receipts ADD COLUMN {name} {definition}"
                        )

            self._apply_schema()
            if upgrading:
                self._collapse_duplicates()
            # Last: it cannot exist until the rows are backfilled and de-duplicated.
            connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {DEDUPE_INDEX} ON receipts(dedupe_key)"
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:
            pass

    # -- writes ----------------------------------------------------------
    def save(self, record: dict[str, Any], record_id: int | None = None) -> SaveResult:
        """Insert, or update the row this entry belongs to.

        An entry belongs to the row sharing its dedupe_key, so saving the same
        purchase twice updates one row instead of counting it twice. With
        `record_id` the caller is editing a saved row: that row is updated in
        place, and if the edit makes it match a *different* row the two are
        merged onto that other row, the one being edited going away.
        """
        payload = {key: record.get(key) for key in COLUMNS}
        payload["dedupe_key"] = dedupe_key(payload)
        payload["updated_at"] = now = _now()

        with self._transaction():
            match = None
            if payload["dedupe_key"] is not None:
                match = self._connection.execute(
                    "SELECT id, notes FROM receipts WHERE dedupe_key=?",
                    (payload["dedupe_key"],),
                ).fetchone()

            target = record_id
            if match is not None and match["id"] != record_id:
                target = match["id"]
                if record_id is not None:  # an edit collided with another row
                    self._connection.execute(
                        "DELETE FROM receipts WHERE id=?", (record_id,)
                    )
                # A repeat that brings no notes of its own must not erase the
                # ones already written. (An in-place edit is different: there,
                # clearing the notes is the user's deliberate act.)
                if not (payload.get("notes") or "").strip():
                    payload["notes"] = match["notes"]

            if target is not None:
                assignments = ", ".join(f"{key}=:{key}" for key in payload)
                cursor = self._connection.execute(
                    f"UPDATE receipts SET {assignments} WHERE id=:id",
                    {**payload, "id": target},
                )
                if cursor.rowcount:
                    return SaveResult(int(target), False)

            payload["created_at"] = now
            names = ", ".join(payload)
            placeholders = ", ".join(f":{key}" for key in payload)
            cursor = self._connection.execute(
                f"INSERT INTO receipts ({names}) VALUES ({placeholders})", payload
            )
            return SaveResult(int(cursor.lastrowid), True)

    def delete(self, record_id: int) -> None:
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
