"""Opening databases created by older versions of the app."""

import sqlite3

from helpers import make_v1_database
from receiptscanner.db import COLUMNS, SCHEMA_VERSION, V2_COLUMNS, ReceiptStore


def columns(path):
    connection = sqlite3.connect(str(path))
    try:
        return [row[1] for row in connection.execute("PRAGMA table_info(receipts)")]
    finally:
        connection.close()


def rows(path):
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(r) for r in connection.execute("SELECT * FROM receipts ORDER BY id")
        ]
    finally:
        connection.close()


V1_ROWS = [
    {
        "name": "Warung A",
        "amount": 25000.0,
        "entry_date": "2026-06-01",
        "currency": "IDR",
    },
    {
        "name": "Warung B",
        "amount": 40000.0,
        "entry_date": "2026-06-02",
        "currency": "IDR",
    },
]


class TestV1ToCurrent:
    def test_missing_columns_are_added(self, tmp_path):
        path = make_v1_database(tmp_path / "old.db", V1_ROWS)
        assert "email_message_id" not in columns(path)

        ReceiptStore(path).close()

        present = columns(path)
        for name, _ in V2_COLUMNS:
            assert name in present
        assert set(COLUMNS) <= set(present)

    def test_existing_rows_survive_untouched(self, tmp_path):
        path = make_v1_database(tmp_path / "old.db", V1_ROWS)
        ReceiptStore(path).close()

        kept = rows(path)
        assert [(r["name"], r["amount"]) for r in kept] == [
            ("Warung A", 25000.0),
            ("Warung B", 40000.0),
        ]
        assert kept[0]["created_at"] == "2026-01-01T00:00:00+07:00"

    def test_old_rows_read_as_plain_images(self, tmp_path):
        path = make_v1_database(tmp_path / "old.db", V1_ROWS)
        ReceiptStore(path).close()
        assert {r["source_kind"] for r in rows(path)} <= {"image", None}

    def test_the_version_is_stamped(self, tmp_path):
        path = make_v1_database(tmp_path / "old.db", V1_ROWS)
        ReceiptStore(path).close()

        connection = sqlite3.connect(str(path))
        try:
            assert (
                connection.execute("PRAGMA user_version").fetchone()[0]
                == SCHEMA_VERSION
            )
        finally:
            connection.close()

    def test_opening_twice_changes_nothing(self, tmp_path):
        path = make_v1_database(tmp_path / "old.db", V1_ROWS)
        ReceiptStore(path).close()
        once_columns, once_rows = columns(path), rows(path)

        ReceiptStore(path).close()

        assert columns(path) == once_columns
        assert rows(path) == once_rows

    def test_a_migrated_database_accepts_new_records(self, tmp_path):
        from helpers import make_record

        path = make_v1_database(tmp_path / "old.db", V1_ROWS)
        store = ReceiptStore(path)
        try:
            store.save(
                make_record(name="Fresh", amount=99000.0, email_message_id="<n@x>")
            )
            assert store.find_by_message_id("<n@x>")["name"] == "Fresh"
            assert store.summary()[0] == 3
        finally:
            store.close()
