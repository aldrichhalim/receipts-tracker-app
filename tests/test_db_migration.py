"""Opening databases created by older versions of the app."""

import re
import sqlite3

import pytest

from helpers import make_v1_database, make_v2_database
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


class TestV3DedupeMigration:
    """Opening an older database: back it up, collapse the duplicates, index it."""

    GRAB = {
        "name": "Grab",
        "amount": 44000.0,
        "entry_date": "2026-07-14",
        "currency": "IDR",
    }

    def rows_v2(self, extra=()):
        return [
            {**self.GRAB, "notes": "kept"},  # id 1 (survivor)
            {
                "name": "Warung",
                "amount": 25000.0,
                "entry_date": "2026-07-01",
                "currency": "IDR",
            },
            {**self.GRAB, "notes": "dropped"},  # id 3 (duplicate of 1)
            {**self.GRAB, "currency": "USD"},  # id 4: same figures, other currency
            *extra,
        ]

    @pytest.fixture
    def old(self, tmp_path):
        return make_v2_database(tmp_path / "receipts.db", self.rows_v2())

    def backups(self, path):
        # Every file, sidecars included: SQLite leaves "-shm"/"-wal" files beside
        # a copy of a WAL database, and a tidy backup is exactly one file.
        return sorted(path.parent.glob(f"{path.name}.bak-*"))

    def test_a_backup_is_taken_first(self, old):
        ReceiptStore(old).close()
        (backup,) = self.backups(old)
        assert re.fullmatch(r"receipts\.db\.bak-\d{8}-\d{6}", backup.name)

    def test_the_backup_is_the_untouched_original(self, old):
        ReceiptStore(old).close()
        (backup,) = self.backups(old)
        assert "dedupe_key" not in columns(backup)
        assert len(rows(backup)) == 4
        assert [r["notes"] for r in rows(backup)][:3] == ["kept", None, "dropped"]

    def test_duplicates_collapse_into_the_oldest_row(self, old):
        ReceiptStore(old).close()
        kept = rows(old)
        assert [r["id"] for r in kept] == [1, 2, 4]  # id 3 folded into id 1
        assert kept[0]["notes"] == "kept"

    def test_a_different_currency_is_not_a_duplicate(self, old):
        ReceiptStore(old).close()
        assert {r["currency"] for r in rows(old) if r["name"] == "Grab"} == {
            "IDR",
            "USD",
        }

    def test_empty_notes_are_filled_from_a_discarded_duplicate(self, tmp_path):
        path = make_v2_database(
            tmp_path / "r.db",
            [{**self.GRAB, "notes": None}, {**self.GRAB, "notes": "from the copy"}],
        )
        ReceiptStore(path).close()
        (only,) = rows(path)
        assert only["notes"] == "from the copy"

    def test_a_group_of_three_collapses_to_one(self, tmp_path):
        path = make_v2_database(tmp_path / "r.db", [self.GRAB, self.GRAB, self.GRAB])
        ReceiptStore(path).close()
        assert [r["id"] for r in rows(path)] == [1]

    def test_the_survivor_keeps_its_own_provenance_and_dates(self, tmp_path):
        a = {
            **self.GRAB,
            "source_kind": "email_render",
            "email_message_id": "<first>",
            "created_at": "2026-01-01T00:00:00+07:00",
        }
        b = {**self.GRAB, "source_kind": "email", "email_message_id": "<second>"}
        path = make_v2_database(tmp_path / "r.db", [a, b])
        ReceiptStore(path).close()
        (only,) = rows(path)
        assert (only["email_message_id"], only["created_at"]) == (
            "<first>",
            "2026-01-01T00:00:00+07:00",
        )

    def test_untitled_rows_are_never_collapsed(self, tmp_path):
        blank = {
            "name": "",
            "amount": 5.0,
            "entry_date": "2026-07-14",
            "currency": "IDR",
        }
        path = make_v2_database(tmp_path / "r.db", [blank, blank, blank])
        ReceiptStore(path).close()
        assert len(rows(path)) == 3

    def test_the_index_and_version_are_in_place(self, old):
        ReceiptStore(old).close()
        connection = sqlite3.connect(str(old))
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_receipts_dedupe'"
            ).fetchone()[0]
            assert "UNIQUE" in sql.upper()
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()

    def test_every_titled_row_gets_a_key(self, old):
        ReceiptStore(old).close()
        assert all(r["dedupe_key"] for r in rows(old))

    def test_a_second_open_changes_nothing_and_takes_no_second_backup(self, old):
        ReceiptStore(old).close()
        snapshot = rows(old)
        ReceiptStore(old).close()
        assert rows(old) == snapshot
        assert len(self.backups(old)) == 1

    def test_the_oldest_v1_shape_migrates_straight_to_v3(self, tmp_path):
        path = make_v1_database(tmp_path / "r.db", [self.GRAB, self.GRAB])
        ReceiptStore(path).close()
        assert len(rows(path)) == 1
        assert len(self.backups(path)) == 1

    def test_a_fresh_database_needs_no_backup(self, tmp_path):
        ReceiptStore(tmp_path / "fresh.db").close()
        assert self.backups(tmp_path / "fresh.db") == []

    def test_an_empty_existing_database_needs_no_backup(self, tmp_path):
        path = make_v2_database(tmp_path / "empty.db", [])
        ReceiptStore(path).close()
        assert self.backups(path) == []

    def test_a_failure_rolls_everything_back_but_keeps_the_backup(
        self, old, monkeypatch
    ):
        def explode(self):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(ReceiptStore, "_collapse_duplicates", explode)
        with pytest.raises(RuntimeError):
            ReceiptStore(old)

        assert "dedupe_key" not in columns(old)  # the ALTER was rolled back too
        assert len(rows(old)) == 4  # nothing deleted
        connection = sqlite3.connect(str(old))
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        finally:
            connection.close()
        assert len(self.backups(old)) == 1

    def test_a_migrated_database_dedupes_new_saves(self, old):
        from helpers import make_record

        store = ReceiptStore(old)
        try:
            again = store.save(
                make_record(
                    **{
                        k: self.GRAB[k]
                        for k in ("name", "amount", "entry_date", "currency")
                    }
                )
            )
            assert again.created is False
            assert again.id == 1
        finally:
            store.close()
