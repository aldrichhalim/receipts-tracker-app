"""Builders shared by several test modules."""

from __future__ import annotations

import mailbox
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from receiptscanner.db import COLUMNS


def make_record(**overrides: Any) -> dict[str, Any]:
    """A complete ReceiptStore record: every column present, sensible values."""
    record: dict[str, Any] = {column: None for column in COLUMNS}
    record.update(
        source_kind="image",
        source_path="/tmp/receipt.jpg",
        source_filename="receipt.jpg",
        entry_date="2026-07-14",
        category="Transportasi",
        name="Grab",
        amount=44000.0,
        currency="IDR",
        notes="",
    )
    record.update(overrides)
    return record


def build_mbox(path: Path, messages: list[EmailMessage]) -> Path:
    box = mailbox.mbox(str(path))
    try:
        for message in messages:
            box.add(message)
        box.flush()
    finally:
        box.close()
    return path


def make_message(
    subject: str = "Your receipt",
    sender: str = "Shop <shop@example.com>",
    plain: str | None = None,
    html: str | None = None,
    message_id: str | None = "<id-1@example.com>",
    when: datetime | None = datetime(2026, 7, 14, 9, 30, tzinfo=timezone.utc),
    images: list[tuple[str, bytes]] | None = None,
) -> EmailMessage:
    """An email with optional plain/html alternatives and image attachments."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    if message_id is not None:
        message["Message-ID"] = message_id
    if when is not None:
        message["Date"] = format_datetime(when)

    if plain is not None:
        message.set_content(plain)
        if html is not None:
            message.add_alternative(html, subtype="html")
    elif html is not None:
        message.set_content(html, subtype="html")
    else:
        message.set_content("")

    for filename, payload in images or []:
        subtype = Path(filename).suffix.lstrip(".").lower() or "jpeg"
        message.add_attachment(
            payload, maintype="image", subtype=subtype, filename=filename
        )
    return message


def row_id(saved: Any) -> int:
    """The id out of whatever ReceiptStore.save() returns.

    save() returned a bare int; it will return a (id, created) result once it
    becomes an upsert. Routing through here keeps the characterisation tests
    valid on both sides of that change instead of being edited to fit it.
    """
    return int(getattr(saved, "id", saved))


V1_DDL = """
CREATE TABLE receipts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path       TEXT NOT NULL,
    source_filename   TEXT,
    scanned_path      TEXT,
    archived_path     TEXT,
    ocr_text          TEXT,
    ocr_text_raw      TEXT,
    ocr_lang          TEXT,
    ocr_confidence    REAL,
    ocr_word_count    INTEGER,
    entry_date        TEXT,
    category          TEXT,
    name              TEXT,
    amount            REAL,
    currency          TEXT,
    notes             TEXT,
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
CREATE INDEX idx_receipts_sha256 ON receipts(source_sha256);
CREATE INDEX idx_receipts_date   ON receipts(entry_date);
CREATE INDEX idx_receipts_cat    ON receipts(category);
"""


def _insert_rows(connection, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        values = {
            "source_path": "/tmp/old.jpg",
            "created_at": "2026-01-01T00:00:00+07:00",
            "updated_at": "2026-01-01T00:00:00+07:00",
            **row,
        }
        names = ", ".join(values)
        marks = ", ".join(f":{key}" for key in values)
        connection.execute(f"INSERT INTO receipts ({names}) VALUES ({marks})", values)


def make_v1_database(path: Path, rows: list[dict[str, Any]]) -> Path:
    """A database shaped like the oldest ones in the wild: no email columns,
    `source_path NOT NULL`, user_version 0. This is the live DB's shape."""
    import sqlite3

    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(V1_DDL)
        _insert_rows(connection, rows)
        connection.commit()
    finally:
        connection.close()
    return path
