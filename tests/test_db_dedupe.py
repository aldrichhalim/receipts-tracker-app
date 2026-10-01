"""One row per (title, amount, currency, date): a repeat updates, never doubles.

Written before the implementation (red first).
"""

import sqlite3

import pytest

from helpers import make_record, row_id
from receiptscanner import db
from receiptscanner.db import ReceiptStore, dedupe_key


def key(name="Grab", amount=44000.0, currency="IDR", date="2026-07-14"):
    return dedupe_key(
        {"name": name, "amount": amount, "currency": currency, "entry_date": date}
    )


@pytest.fixture
def clock(monkeypatch):
    ticks = iter(f"2026-07-14T10:00:{n:02d}+07:00" for n in range(60))
    monkeypatch.setattr(db, "_now", lambda: next(ticks))


class TestDedupeKey:
    def test_identical_records_share_a_key(self):
        assert key() == key()

    @pytest.mark.parametrize("variant", [{"name": "  grab  "}, {"name": "GRAB"}])
    def test_case_and_whitespace_do_not_split_a_match(self, variant):
        assert key(**variant) == key()

    def test_inner_whitespace_runs_collapse(self):
        assert key(name="Nasi   Goreng\tPak  De") == key(name="nasi goreng pak de")

    def test_full_width_and_compatibility_forms_match(self):
        assert key(name="ＧＲＡＢ") == key(name="Grab")

    def test_unicode_casefolding(self):
        assert key(name="Straße") == key(name="STRASSE")

    @pytest.mark.parametrize(
        "a, b",
        [(44000, 44000.0), (0.1 + 0.2, 0.3), (19.999999999, 20.0), ("44000", 44000.0)],
    )
    def test_float_noise_does_not_split_a_match(self, a, b):
        assert key(amount=a) == key(amount=b)

    def test_a_one_cent_difference_does_split(self):
        assert key(amount=44000.00) != key(amount=44000.01)

    @pytest.mark.parametrize(
        "other",
        [
            {"name": "Gojek"},
            {"amount": 45000.0},
            {"currency": "USD"},
            {"date": "2026-07-15"},
        ],
    )
    def test_any_differing_part_gives_a_different_key(self, other):
        assert key(**other) != key()

    def test_currency_case_is_ignored(self):
        assert key(currency="idr") == key(currency="IDR")

    def test_the_same_figure_in_two_currencies_is_two_entries(self):
        assert key(amount=20.0, currency="IDR") != key(amount=20.0, currency="USD")

    def test_a_title_containing_separators_cannot_forge_a_collision(self):
        forged = key(name="Grab|4400000|IDR|2026-07-14", amount=1.0, date="2026-01-01")
        assert forged != key(name="Grab", amount=44000.0, date="2026-07-14")
        assert key(name="a|b") != key(name="a", currency="b")

    @pytest.mark.parametrize("name", [None, "", "   ", "\t\n"])
    def test_no_title_means_no_key(self, name):
        # Two untitled receipts sharing an amount and date are not the same
        # purchase, so they must never be merged.
        assert key(name=name) is None

    @pytest.mark.parametrize("amount", [None, "", "abc"])
    def test_no_readable_amount_means_no_key(self, amount):
        assert key(amount=amount) is None

    @pytest.mark.parametrize("date", [None, ""])
    def test_no_date_means_no_key(self, date):
        assert key(date=date) is None

    def test_a_missing_currency_still_keys(self):
        assert key(currency=None) is not None
        assert key(currency=None) == key(currency="")


class TestUpsert:
    def test_the_first_save_creates_the_second_updates(self, store):
        first = store.save(make_record())
        second = store.save(make_record())

        assert first.created is True and second.created is False
        assert first.id == second.id
        assert len(store.recent()) == 1

    def test_the_result_unpacks_as_id_and_created(self, store):
        rid, created = store.save(make_record())
        assert created is True and rid == store.recent()[0]["id"]

    def test_case_and_whitespace_variants_hit_the_same_row(self, store):
        store.save(make_record(name="Grab"))
        again = store.save(make_record(name="  GRAB "))
        assert again.created is False
        assert len(store.recent()) == 1

    @pytest.mark.parametrize(
        "change",
        [
            {"amount": 45000.0},
            {"currency": "USD"},
            {"entry_date": "2026-07-15"},
            {"name": "Gojek"},
        ],
    )
    def test_any_differing_part_is_a_new_entry(self, store, change):
        store.save(make_record())
        assert store.save(make_record(**change)).created is True
        assert len(store.recent()) == 2

    def test_a_repeat_updates_the_other_fields_in_place(self, store):
        rid = row_id(store.save(make_record(category="Lainnya")))
        store.save(make_record(category="Transportasi", ocr_confidence=91.5))

        row = store.recent()[0]
        assert row["id"] == rid
        assert (row["category"], row["ocr_confidence"]) == ("Transportasi", 91.5)

    def test_created_at_survives_and_updated_at_moves(self, store, clock):
        store.save(make_record())
        before = store.recent()[0]
        store.save(make_record(notes="edited"))
        after = store.recent()[0]
        assert after["created_at"] == before["created_at"]
        assert after["updated_at"] > before["updated_at"]

    @pytest.mark.parametrize("empty", [None, ""])
    def test_existing_notes_survive_an_update_with_empty_notes(self, store, empty):
        store.save(make_record(notes="lunch with the team"))
        store.save(make_record(notes=empty))
        assert store.recent()[0]["notes"] == "lunch with the team"

    def test_new_notes_replace_old_ones(self, store):
        store.save(make_record(notes="old"))
        store.save(make_record(notes="new"))
        assert store.recent()[0]["notes"] == "new"

    def test_untitled_receipts_are_never_merged(self, store):
        store.save(make_record(name=""))
        assert store.save(make_record(name="")).created is True
        assert len(store.recent()) == 2

    def test_the_same_email_with_a_different_title_is_not_merged(self, store):
        # A documented limit: the key is the title, not the Message-ID.
        store.save(make_record(name="Grab A-9J6", email_message_id="<m@x>"))
        assert store.save(make_record(name="Grab", email_message_id="<m@x>")).created

    def test_many_repeats_stay_one_row(self, store):
        for _ in range(25):
            store.save(make_record())
        assert len(store.recent()) == 1


class TestEditingASavedRow:
    def test_editing_in_place_keeps_the_id(self, store):
        rid = row_id(store.save(make_record(notes="a")))
        result = store.save(make_record(notes="b"), record_id=rid)
        assert (result.id, result.created) == (rid, False)
        assert store.recent()[0]["notes"] == "b"

    def test_clearing_the_notes_in_place_sticks(self, store):
        # Matching by key keeps old notes when the new ones are empty, but an
        # explicit edit of a saved row is the user clearing them on purpose.
        rid = row_id(store.save(make_record(notes="remove me")))
        store.save(make_record(notes=""), record_id=rid)
        assert not store.recent()[0]["notes"]

    def test_a_stale_record_id_inserts_instead_of_vanishing(self, store):
        result = store.save(make_record(), record_id=9999)
        assert result.created is True
        assert len(store.recent()) == 1

    def test_changing_the_amount_moves_the_key_with_it(self, store):
        rid = row_id(store.save(make_record(amount=1000.0)))
        store.save(make_record(amount=2000.0), record_id=rid)

        # The new values now identify the row...
        assert store.save(make_record(amount=2000.0)).id == rid
        assert len(store.recent()) == 1

    def test_and_the_old_values_are_released(self, store):
        rid = row_id(store.save(make_record(amount=1000.0)))
        store.save(make_record(amount=2000.0), record_id=rid)

        fresh = store.save(make_record(amount=1000.0))
        assert fresh.created is True and fresh.id != rid

    def test_editing_a_row_into_another_merges_onto_the_survivor(self, store):
        keep = row_id(store.save(make_record(name="Grab", amount=1000.0)))
        edit = row_id(store.save(make_record(name="Other", amount=2000.0)))

        result = store.save(
            make_record(name="Grab", amount=1000.0, notes="merged"), record_id=edit
        )

        assert (result.id, result.created) == (keep, False)
        rows = store.recent()
        assert [r["id"] for r in rows] == [keep]  # the edited row is gone
        assert rows[0]["notes"] == "merged"

    def test_a_merge_leaves_unrelated_rows_alone(self, store):
        row_id(store.save(make_record(name="Grab", amount=1000.0)))
        edit = row_id(store.save(make_record(name="Other", amount=2000.0)))
        bystander = row_id(store.save(make_record(name="Bystander", amount=3000.0)))

        store.save(make_record(name="Grab", amount=1000.0), record_id=edit)
        assert bystander in {r["id"] for r in store.recent()}
        assert len(store.recent()) == 2


class TestTheIndexIsTheBackstop:
    def test_a_unique_index_exists(self, store):
        row = store._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_receipts_dedupe'"
        ).fetchone()
        assert row is not None and "UNIQUE" in row["sql"].upper()

    def test_a_raw_duplicate_insert_is_rejected_by_the_database(self, store):
        store.save(make_record())
        stored = store._connection.execute(
            "SELECT dedupe_key FROM receipts"
        ).fetchone()[0]
        assert stored == key()

        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                "INSERT INTO receipts (source_path, created_at, updated_at, dedupe_key) "
                "VALUES ('x', 'now', 'now', ?)",
                (stored,),
            )

    def test_many_untitled_rows_do_not_trip_the_index(self, store):
        for n in range(3):
            store.save(make_record(name="", amount=float(n + 1)))
        assert len(store.recent()) == 3

    def test_a_failed_upsert_leaves_nothing_behind(self, store, monkeypatch):
        store.save(make_record())
        monkeypatch.setattr(
            db, "_now", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        with pytest.raises(RuntimeError):
            store.save(make_record(amount=99999.0))
        monkeypatch.undo()
        assert len(store.recent()) == 1


class TestAtomicity:
    def test_a_failed_merge_rolls_back_the_delete_and_leaves_the_store_usable(
        self, store
    ):
        """The migration test cannot cover this: there the connection is closed on
        failure, and closing rolls back by itself. Here it stays open, so only an
        explicit ROLLBACK stands between a half-done merge and a stuck connection."""
        keep = row_id(store.save(make_record(name="a", amount=1000.0)))
        edit = row_id(store.save(make_record(name="b", amount=2000.0)))
        store._connection.execute(
            "CREATE TRIGGER boom BEFORE UPDATE ON receipts WHEN NEW.amount = 1000 "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )

        with pytest.raises(sqlite3.DatabaseError):
            store.save(make_record(name="a", amount=1000.0), record_id=edit)

        assert {r["id"] for r in store.recent()} == {keep, edit}  # the DELETE undone
        store._connection.execute("DROP TRIGGER boom")
        assert store.save(make_record(name="c", amount=3.0)).created is True
