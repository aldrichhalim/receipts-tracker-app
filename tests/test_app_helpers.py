"""Module-level helpers in app.py that need no window."""

import pytest

from receiptscanner.app import _format_money, parse_date_input


class TestParseDateInput:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("2026-07-14", "2026-07-14"),
            ("14/07/2026", "2026-07-14"),
            ("14-07-2026", "2026-07-14"),
            ("14.07.2026", "2026-07-14"),
            ("2026/07/14", "2026-07-14"),
            ("14/07/26", "2026-07-14"),
            ("  14/07/2026  ", "2026-07-14"),
            ("03/04/2026", "2026-04-03"),  # day-first by default
        ],
    )
    def test_accepted_formats(self, raw, expected):
        assert parse_date_input(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["", None, "   ", "not a date", "31/02/2026", "2026-13-01"]
    )
    def test_unreadable_input_is_none(self, raw):
        assert parse_date_input(raw) is None


class TestParseDateInputForDollars:
    def test_usd_reads_month_first(self):
        assert parse_date_input("03/04/2026", "USD") == "2026-03-04"
        assert parse_date_input("12/25/2026", "USD") == "2026-12-25"

    def test_usd_still_accepts_an_unambiguous_day_first_date(self):
        assert parse_date_input("25/12/2026", "USD") == "2026-12-25"

    def test_idr_is_unchanged(self):
        assert parse_date_input("03/04/2026", "IDR") == "2026-04-03"

    @pytest.mark.parametrize("raw", ["Aug 17, 2026", "August 17, 2026"])
    @pytest.mark.parametrize("currency", ["", "IDR", "USD"])
    def test_month_names_parse_for_every_currency(self, raw, currency):
        assert parse_date_input(raw, currency) == "2026-08-17"


class TestFormatMoney:
    @pytest.mark.parametrize(
        "amount, currency, expected",
        [
            (44000.0, "IDR", "IDR 44,000"),
            (12.5, "USD", "USD 12.50"),
            (1234567.0, "IDR", "IDR 1,234,567"),
            (44000.0, "", "44,000"),
        ],
    )
    def test_formatting(self, amount, currency, expected):
        assert _format_money(amount, currency) == expected


class TestFormatTotals:
    def test_one_figure_per_currency(self):
        from receiptscanner.app import _format_totals

        assert (
            _format_totals({"USD": 76.48, "IDR": 69000.0}) == "IDR 69,000 · USD 76.48"
        )

    def test_a_single_currency(self):
        from receiptscanner.app import _format_totals

        assert _format_totals({"IDR": 44000.0}) == "IDR 44,000"

    def test_nothing(self):
        from receiptscanner.app import _format_totals

        assert _format_totals({}) == ""
