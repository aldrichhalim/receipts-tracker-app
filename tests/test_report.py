"""CSV export and date presets (report.py has no Tk dependency by design)."""

import csv
from datetime import date

import pytest

from helpers import make_record
from receiptscanner import report


def read_csv(path):
    """Rows as dicts keyed by header, decoded the way Excel decodes the BOM."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def entry(**overrides):
    row = {
        "entry_date": "2026-07-14",
        "category": "Transportasi",
        "name": "Grab",
        "amount": 44000.0,
        "currency": "IDR",
    }
    row.update(overrides)
    return row


class TestSummarize:
    def test_counts_and_totals(self):
        summary = report.summarize(
            [entry(amount=1000.0), entry(amount=2500.5)], "2026-07-01", "2026-07-31"
        )
        assert summary.count == 2
        assert summary.total == pytest.approx(3500.5)
        assert (summary.start, summary.end) == ("2026-07-01", "2026-07-31")

    def test_empty_input(self):
        summary = report.summarize([], "2026-07-01", "2026-07-31")
        assert (summary.count, summary.total) == (0, 0.0)

    @pytest.mark.parametrize("bad", [None, "", "abc", "n/a"])
    def test_unreadable_amounts_are_counted_but_add_nothing(self, bad):
        summary = report.summarize([entry(amount=1000.0), entry(amount=bad)], "a", "b")
        assert summary.count == 2
        assert summary.total == pytest.approx(1000.0)

    def test_amounts_stored_as_text_are_still_summed(self):
        assert report.summarize([entry(amount="1500")], "a", "b").total == 1500.0

    def test_cents_do_not_drift(self):
        rows = [entry(amount=0.1), entry(amount=0.2), entry(amount=0.3)]
        assert report.summarize(rows, "a", "b").total == pytest.approx(0.6)

    def test_the_summary_is_immutable(self):
        summary = report.summarize([], "a", "b")
        with pytest.raises(Exception):
            summary.count = 5


class TestWriteCsv:
    def test_returns_the_number_of_rows_written(self, tmp_path):
        rows = [entry(), entry(name="Other")]
        assert report.write_csv(rows, tmp_path / "out.csv") == 2

    def test_file_starts_with_a_utf8_bom_so_excel_reads_it(self, tmp_path):
        path = tmp_path / "out.csv"
        report.write_csv([entry()], path)
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_columns_and_values(self, tmp_path):
        path = tmp_path / "out.csv"
        report.write_csv([entry()], path)
        row = read_csv(path)[0]
        assert row["Date"] == "2026-07-14"
        assert row["Category"] == "Transportasi"
        assert row["Expense Detail"] == "Grab"
        assert row["Amount"] == "44000.00"

    def test_amounts_are_unformatted_so_a_spreadsheet_can_sum_them(self, tmp_path):
        path = tmp_path / "out.csv"
        report.write_csv([entry(amount=1234567.891)], path)
        assert read_csv(path)[0]["Amount"] == "1234567.89"

    @pytest.mark.parametrize(
        "amount, expected",
        [(None, "0.00"), ("", "0.00"), ("abc", ""), ("12.5", "12.50")],
    )
    def test_odd_amounts(self, tmp_path, amount, expected):
        path = tmp_path / "out.csv"
        report.write_csv([entry(amount=amount)], path)
        assert read_csv(path)[0]["Amount"] == expected

    def test_missing_text_becomes_empty_cells(self, tmp_path):
        path = tmp_path / "out.csv"
        report.write_csv([entry(entry_date=None, category=None, name=None)], path)
        row = read_csv(path)[0]
        assert (row["Date"], row["Category"], row["Expense Detail"]) == ("", "", "")

    def test_missing_parent_directories_are_created(self, tmp_path):
        path = tmp_path / "a" / "b" / "out.csv"
        report.write_csv([entry()], path)
        assert path.is_file()

    def test_no_rows_still_writes_a_header(self, tmp_path):
        path = tmp_path / "out.csv"
        assert report.write_csv([], path) == 0
        assert read_csv(path) == []
        assert path.read_text(encoding="utf-8-sig").strip() != ""

    @pytest.mark.parametrize(
        "name",
        [
            "Warung, Pak De",
            'He said "enak"',
            "two\nlines",
            "Nasi Goreng — Spesial ☕",
            "日本語",
        ],
    )
    def test_awkward_text_round_trips(self, tmp_path, name):
        path = tmp_path / "out.csv"
        report.write_csv([entry(name=name)], path)
        assert read_csv(path)[0]["Expense Detail"] == name

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "KNOWN BUG, CSV formula injection: a title is OCR output or an email "
            "sender, i.e. untrusted, and write_csv emits it verbatim, so one that "
            "starts with = + - @ is executed as a formula when opened in Excel. "
            "Fix by prefixing such cells with a single quote; remove this xfail then."
        ),
    )
    @pytest.mark.parametrize("name", ["=SUM(A1)", "+1+1", "-2+3", "@SUM(A1)"])
    def test_formula_cells_are_neutralised(self, tmp_path, name):
        path = tmp_path / "out.csv"
        report.write_csv([entry(name=name)], path)
        cell = read_csv(path)[0]["Expense Detail"]
        assert not cell.startswith(("=", "+", "-", "@"))

    def test_accepts_database_rows(self, tmp_path, store):
        store.save(make_record(name="From the DB", amount=2500.0))
        path = tmp_path / "out.csv"
        report.write_csv(store.recent(), path)
        assert read_csv(path)[0]["Expense Detail"] == "From the DB"


class TestPresets:
    TODAY = date(2026, 7, 14)

    def test_this_month(self):
        assert report.presets(self.TODAY)["This month"] == ("2026-07-01", "2026-07-14")

    def test_last_month(self):
        assert report.presets(self.TODAY)["Last month"] == ("2026-06-01", "2026-06-30")

    def test_this_year(self):
        assert report.presets(self.TODAY)["This year"] == ("2026-01-01", "2026-07-14")

    def test_january_looks_back_to_december_of_the_previous_year(self):
        assert report.presets(date(2026, 1, 15))["Last month"] == (
            "2025-12-01",
            "2025-12-31",
        )

    def test_leap_february(self):
        assert report.presets(date(2024, 3, 10))["Last month"] == (
            "2024-02-01",
            "2024-02-29",
        )

    def test_first_of_the_month(self):
        assert report.presets(date(2026, 7, 1))["This month"] == (
            "2026-07-01",
            "2026-07-01",
        )

    def test_defaults_to_today(self):
        start, end = report.presets()["This month"]
        assert end == date.today().isoformat()
        assert start == date.today().replace(day=1).isoformat()

    def test_month_start(self):
        assert report.month_start(date(2026, 7, 14)) == date(2026, 7, 1)

    def test_default_filename(self):
        assert (
            report.default_filename("2026-07-01", "2026-07-31")
            == "receipts_2026-07-01_to_2026-07-31.csv"
        )


def test_range_query_summary_and_csv_agree(store, tmp_path):
    """The three stages of an export must describe the same set of rows."""
    for day, amount in (
        ("2026-06-30", 1000.0),
        ("2026-07-01", 2000.0),
        ("2026-07-15", 3000.0),
        ("2026-08-01", 4000.0),
    ):
        store.save(make_record(entry_date=day, name=f"d{day}", amount=amount))

    rows = store.entries_between("2026-07-01", "2026-07-31")
    summary = report.summarize(rows, "2026-07-01", "2026-07-31")
    path = tmp_path / "july.csv"
    written = report.write_csv(rows, path)

    exported = read_csv(path)
    assert summary.count == written == len(exported) == 2
    assert summary.total == pytest.approx(5000.0)
    assert sum(float(r["Amount"]) for r in exported) == pytest.approx(summary.total)


class TestPerCurrency:
    """Summing rupiah and dollars is meaningless, so totals are kept apart."""

    ROWS = [
        entry(amount=44000.0, currency="IDR"),
        entry(amount=25000.0, currency="IDR"),
        entry(amount=56.48, currency="USD"),
        entry(amount=20.0, currency="USD"),
    ]

    def test_totals_are_split_by_currency(self):
        summary = report.summarize(self.ROWS, "a", "b")
        assert summary.count == 4
        assert summary.totals == pytest.approx({"IDR": 69000.0, "USD": 76.48})

    def test_a_single_currency_has_a_single_total(self):
        summary = report.summarize(self.ROWS[:2], "a", "b")
        assert summary.totals == {"IDR": 69000.0}

    def test_rows_with_no_currency_count_towards_the_default(self):
        legacy = [
            entry(amount=1000.0, currency=None),
            entry(amount=2000.0, currency=""),
        ]
        summary = report.summarize(legacy, "a", "b", default_currency="IDR")
        assert summary.totals == {"IDR": 3000.0}

    def test_rows_that_lack_the_key_entirely_are_tolerated(self):
        row = {"entry_date": "2026-07-14", "category": "x", "name": "n", "amount": 5.0}
        assert report.summarize([row], "a", "b", default_currency="IDR").totals == {
            "IDR": 5.0
        }

    def test_unreadable_amounts_add_to_no_currency(self):
        summary = report.summarize([entry(amount="abc", currency="USD")], "a", "b")
        assert summary.totals.get("USD", 0.0) == 0.0

    def test_empty_input_has_no_totals(self):
        assert report.summarize([], "a", "b").totals == {}

    def test_the_csv_has_a_currency_column_after_the_amount(self, tmp_path):
        path = tmp_path / "out.csv"
        report.write_csv(self.ROWS, path)
        with open(path, encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle))
        assert header[:5] == [
            "Date",
            "Category",
            "Expense Detail",
            "Amount",
            "Currency",
        ]

    def test_each_row_carries_its_own_currency(self, tmp_path):
        path = tmp_path / "out.csv"
        report.write_csv(self.ROWS, path)
        rows = read_csv(path)
        assert [(r["Amount"], r["Currency"]) for r in rows] == [
            ("44000.00", "IDR"),
            ("25000.00", "IDR"),
            ("56.48", "USD"),
            ("20.00", "USD"),
        ]

    def test_a_legacy_row_without_a_currency_gets_the_default(self, tmp_path):
        path = tmp_path / "out.csv"
        report.write_csv([entry(currency=None)], path, default_currency="IDR")
        assert read_csv(path)[0]["Currency"] == "IDR"

    def test_the_amount_header_no_longer_claims_one_currency(self, tmp_path):
        path = tmp_path / "out.csv"
        report.write_csv(self.ROWS, path)
        assert "Amount" in read_csv(path)[0]
