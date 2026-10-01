"""ReceiptStore: the plain read/write behaviour the rest of the app relies on."""

import sqlite3

import pytest

from helpers import make_record, row_id
from receiptscanner import db
from receiptscanner.db import COLUMNS, SCHEMA_VERSION, ReceiptStore


@pytest.fixture
def clock(monkeypatch):
    """A deterministic, strictly increasing _now() so timestamps can be compared."""
    ticks = iter(f"2026-07-14T10:00:{n:02d}+07:00" for n in range(60))
    monkeypatch.setattr(db, "_now", lambda: next(ticks))


class TestFreshDatabase:
    def test_schema_version_is_stamped(self, store):
        version = store._connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION

    def test_every_column_exists(self, store):
        present = {
            row["name"]
            for row in store._connection.execute("PRAGMA table_info(receipts)")
        }
        assert set(COLUMNS) <= present

    def test_wal_and_foreign_keys_are_on(self, store):
        assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_the_parent_directory_is_created(self, tmp_path):
        nested = tmp_path / "a" / "b" / "receipts.db"
        ReceiptStore(nested).close()
        assert nested.is_file()


class TestSaveAndRead:
    def test_insert_returns_a_new_id_each_time(self, store):
        first = row_id(store.save(make_record(name="One", amount=1000.0)))
        second = row_id(store.save(make_record(name="Two", amount=2000.0)))
        assert second == first + 1

    def test_reviewed_fields_round_trip(self, store):
        saved = row_id(
            store.save(
                make_record(
                    name="Nasi Goreng",
                    amount=44000.0,
                    entry_date="2026-06-16",
                    category="Makanan & Minuman",
                    currency="IDR",
                    notes="lunch",
                )
            )
        )
        row = store.recent()[0]
        assert row["id"] == saved
        assert (row["name"], row["amount"], row["entry_date"]) == (
            "Nasi Goreng",
            44000.0,
            "2026-06-16",
        )
        assert (row["category"], row["currency"], row["notes"]) == (
            "Makanan & Minuman",
            "IDR",
            "lunch",
        )

    def test_amount_is_stored_as_a_real(self, store):
        store.save(make_record(amount=12.5))
        kind = store._connection.execute(
            "SELECT typeof(amount) FROM receipts"
        ).fetchone()[0]
        assert kind == "real"

    def test_unknown_keys_are_ignored_and_missing_ones_are_null(self, store):
        store.save({"name": "Sparse", "amount": 1.0, "not_a_column": "x"})
        row = store.recent()[0]
        assert row["name"] == "Sparse"
        assert row["category"] is None

    def test_timestamps_are_set_on_insert(self, store):
        from datetime import datetime

        store.save(make_record())
        row = store.recent()[0]
        for column in ("created_at", "updated_at"):
            assert datetime.fromisoformat(row[column]).tzinfo is not None


class TestUpdateInPlace:
    def test_updating_by_id_keeps_the_row_and_changes_the_values(self, store, clock):
        rid = row_id(store.save(make_record(name="Old", amount=1000.0)))
        again = row_id(
            store.save(make_record(name="New", amount=2000.0), record_id=rid)
        )

        assert again == rid
        assert len(store.recent()) == 1
        row = store.recent()[0]
        assert (row["name"], row["amount"]) == ("New", 2000.0)

    def test_update_bumps_updated_at_but_not_created_at(self, store, clock):
        rid = row_id(store.save(make_record()))
        before = store.recent()[0]
        store.save(make_record(notes="edited"), record_id=rid)
        after = store.recent()[0]

        assert after["created_at"] == before["created_at"]
        assert after["updated_at"] > before["updated_at"]


class TestLookups:
    def test_find_by_hash_returns_the_latest_match(self, store):
        store.save(make_record(name="old", source_sha256="abc"))
        newest = row_id(
            store.save(make_record(name="new", amount=2.0, source_sha256="abc"))
        )
        assert store.find_by_hash("abc")["id"] == newest

    def test_find_by_message_id_returns_the_latest_match(self, store):
        store.save(make_record(email_message_id="<m@x>"))
        newest = row_id(store.save(make_record(amount=2.0, email_message_id="<m@x>")))
        assert store.find_by_message_id("<m@x>")["id"] == newest

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_keys_never_match(self, store, empty):
        store.save(make_record(source_sha256=None, email_message_id=None))
        assert store.find_by_hash(empty) is None
        assert store.find_by_message_id(empty) is None

    def test_unknown_keys_are_none(self, store):
        assert store.find_by_hash("nope") is None
        assert store.find_by_message_id("<nope@x>") is None


class TestDateRange:
    @pytest.fixture
    def seeded(self, store):
        for day, amount in (
            ("2026-06-30", 1.0),
            ("2026-07-01", 2.0),
            ("2026-07-15", 3.0),
            ("2026-07-31", 4.0),
            ("2026-08-01", 5.0),
        ):
            store.save(make_record(entry_date=day, name=f"d{day}", amount=amount))
        return store

    def test_both_ends_are_inclusive(self, seeded):
        days = [
            r["entry_date"] for r in seeded.entries_between("2026-07-01", "2026-07-31")
        ]
        assert days == ["2026-07-01", "2026-07-15", "2026-07-31"]

    def test_a_single_day_range(self, seeded):
        assert [
            r["amount"] for r in seeded.entries_between("2026-07-15", "2026-07-15")
        ] == [3.0]

    def test_an_empty_range_is_empty(self, seeded):
        assert seeded.entries_between("2030-01-01", "2030-12-31") == []

    def test_results_are_ordered_by_date_then_id(self, store):
        later = row_id(store.save(make_record(entry_date="2026-07-02", name="later")))
        first = row_id(
            store.save(make_record(entry_date="2026-07-01", name="a", amount=1.0))
        )
        second = row_id(
            store.save(make_record(entry_date="2026-07-01", name="b", amount=2.0))
        )
        ids = [r["id"] for r in store.entries_between("2026-07-01", "2026-07-31")]
        assert ids == [first, second, later]

    @pytest.mark.parametrize("blank", [None, ""])
    def test_undated_rows_are_excluded(self, store, blank):
        store.save(make_record(entry_date=blank))
        assert store.entries_between("0000-01-01", "9999-12-31") == []

    def test_date_bounds(self, seeded):
        assert seeded.date_bounds() == ("2026-06-30", "2026-08-01")

    def test_date_bounds_of_an_empty_store(self, store):
        assert store.date_bounds() == (None, None)

    def test_date_bounds_ignore_undated_rows(self, store):
        store.save(make_record(entry_date=""))
        store.save(make_record(entry_date="2026-07-01", name="x", amount=1.0))
        assert store.date_bounds() == ("2026-07-01", "2026-07-01")


class TestRecentDeleteSummary:
    def test_recent_is_newest_first_and_limited(self, store):
        ids = [
            row_id(store.save(make_record(name=f"n{i}", amount=float(i + 1))))
            for i in range(5)
        ]
        assert [r["id"] for r in store.recent(limit=3)] == ids[::-1][:3]

    def test_delete_removes_the_row(self, store):
        rid = row_id(store.save(make_record()))
        store.delete(rid)
        assert store.recent() == []

    def test_deleting_a_missing_row_is_harmless(self, store):
        store.delete(9999)

    def test_summary_counts_rows(self, store):
        assert store.summary()[0] == 0
        store.save(make_record(name="a", amount=1.0))
        store.save(make_record(name="b", amount=2.0))
        assert store.summary()[0] == 2

    def test_close_is_idempotent(self, tmp_path):
        receipts = ReceiptStore(tmp_path / "x.db")
        receipts.close()
        receipts.close()
        with pytest.raises(sqlite3.ProgrammingError):
            receipts.recent()


class TestSummaryByCurrency:
    def test_totals_are_grouped_by_currency(self, store):
        store.save(make_record(name="a", amount=1000.0, currency="IDR"))
        store.save(make_record(name="b", amount=2000.0, currency="IDR"))
        store.save(make_record(name="c", amount=12.5, currency="USD"))

        count, totals = store.summary()
        assert count == 3
        assert totals == pytest.approx({"IDR": 3000.0, "USD": 12.5})

    def test_an_empty_store_has_no_totals(self, store):
        assert store.summary() == (0, {})

    def test_rows_with_no_currency_are_grouped_under_an_empty_key(self, store):
        store.save(make_record(name="a", amount=5.0, currency=None))
        assert store.summary()[1] == {"": 5.0}

    def test_entries_between_exposes_the_currency(self, store):
        store.save(make_record(name="a", amount=5.0, currency="USD"))
        (row,) = store.entries_between("2026-01-01", "2026-12-31")
        assert row["currency"] == "USD"
