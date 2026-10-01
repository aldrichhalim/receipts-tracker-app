"""USD receipts and emails: currency detection and the USD-aware extractors.

Written before the implementation (red first). The IDR rules stay exactly as
the characterisation tests pin them; everything here is additive.
"""

from decimal import Decimal

import pytest

from receiptscanner.parsing import (
    detect_currency,
    extract_amount,
    extract_date,
    parse_receipt,
)


class TestDetectCurrency:
    @pytest.mark.parametrize(
        "text",
        [
            "Total $9.99",
            "Amount due USD 1,250.00",
            "Sejumlah : USD 56,48",
            "Charged US$ 20.00",
            "total usd 15",  # case-insensitive
            "Subtotal $10.50\nTax $0.84\nTotal $11.34",
        ],
    )
    def test_dollar_markers(self, text):
        assert detect_currency(text) == "USD"

    @pytest.mark.parametrize(
        "text",
        [
            "Total Rp 44.000",
            "TOTAL (INCL. TAX)   Rp 91300",
            "Total Payment : IDR 143,000.00",
            "Sejumlah : Rp54.000,00",
            "Total Rupiah 44.000",
        ],
    )
    def test_rupiah_markers(self, text):
        assert detect_currency(text) == "IDR"

    def test_the_more_frequent_marker_wins_when_both_appear(self):
        # A rupiah receipt that mentions a dollar fare once is still rupiah.
        assert detect_currency("Rp 10.000\nRp 20.000\nRp 30.000\nUSD 1.00") == "IDR"
        assert detect_currency("$1.00\n$2.00\n$3.00\nRp 10.000") == "USD"

    def test_a_tie_or_no_marker_is_undecided(self):
        assert detect_currency("Total 44.000") == ""
        assert detect_currency("USD 1.00\nRp 10.000") == ""
        assert detect_currency("") == ""

    def test_the_caller_supplied_default_fills_the_gap(self):
        assert detect_currency("Total 44.000", default="IDR") == "IDR"
        assert detect_currency("Total 44.000", default="USD") == "USD"
        assert detect_currency("Total $9.99", default="IDR") == "USD"

    @pytest.mark.parametrize("text", ["BUSD 5", "$ alone", "a $ b", "S$ 5", "USDA"])
    def test_lookalikes_are_not_dollars(self, text):
        assert detect_currency(text) == ""


class TestUsdAmounts:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("Total $9.99", "9.99"),
            ("Total $0.99", "0.99"),
            ("Subtotal $10.50\nTax $0.84\nTotal $11.34", "11.34"),
            ("Amount due USD 1,250.00", "1250.00"),
            ("Sejumlah : USD 56,48", "56.48"),  # BCA: a decimal comma
            ("Sejumlah : USD 20", "20"),  # a whole-dollar charge
            ("Sejumlah : USD 15", "15"),
            ("Order total $42.10", "42.10"),
            ("Total charged $42.10", "42.10"),
            ("Balance due $12.00", "12.00"),
            ("Total due $12.00", "12.00"),
            ("Amount paid $12.00", "12.00"),
        ],
    )
    def test_small_dollar_totals_are_found(self, text, expected):
        assert extract_amount(text, "USD") == Decimal(expected)

    def test_discounts_tips_and_tax_lines_do_not_win(self):
        text = "Item $8.00\nDiscount $1.00\nTax $0.64\nTip $2.00\nTotal $9.64"
        assert extract_amount(text, "USD") == Decimal("9.64")

    def test_a_bare_quantity_is_not_a_dollar_total(self):
        assert extract_amount("Qty 2\nTotal 3", "USD") is None

    def test_a_card_suffix_is_not_a_dollar_total(self):
        assert extract_amount("Visa **** 4242\nTotal $19.99", "USD") == Decimal("19.99")

    def test_the_total_tier_rules_still_apply(self):
        assert extract_amount("Subtotal $60.00\nTotal $55.00", "USD") == Decimal(
            "55.00"
        )

    def test_a_usd_receipt_is_found_without_an_explicit_currency_too(self):
        # The extractor can also work it out from the markers on the line.
        assert extract_amount("Total $9.99") == Decimal("9.99")


class TestIdrIsUnchanged:
    """The small-value floor is an IDR rule: it must not leak into USD handling."""

    @pytest.mark.parametrize("text", ["Total 9.99", "Total 50", "Total 3"])
    def test_small_idr_values_are_still_ignored(self, text):
        assert extract_amount(text) is None
        assert extract_amount(text, "IDR") is None

    def test_default_currency_is_idr(self):
        assert extract_amount("Total 44.000") == extract_amount("Total 44.000", "IDR")


class TestEnglishDates:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("Aug 17, 2026", "2026-08-17"),
            ("August 17, 2026", "2026-08-17"),
            ("July 14, 2026", "2026-07-14"),
            ("Jul 4 2026", "2026-07-04"),
            ("Sept 9, 2026", "2026-09-09"),
            ("May 5, 2026", "2026-05-05"),
            ("December 25th, 2026", "2026-12-25"),
            ("Paid on Aug. 17, 2026 at 3pm", "2026-08-17"),
        ],
    )
    def test_month_first_names_parse_for_both_currencies(self, text, expected):
        assert extract_date(text) == expected
        assert extract_date(text, "USD") == expected

    def test_day_first_names_still_work(self):
        assert extract_date("14 July 2026", "USD") == "2026-07-14"

    @pytest.mark.parametrize("text", ["Smarch 5, 2026", "August 45, 2026"])
    def test_nonsense_is_rejected(self, text):
        assert extract_date(text, "USD") is None


class TestNumericDateOrder:
    def test_usd_reads_month_first(self):
        assert extract_date("03/04/2026", "USD") == "2026-03-04"
        assert extract_date("12/25/2026", "USD") == "2026-12-25"

    def test_usd_falls_back_to_day_first_when_month_first_is_impossible(self):
        assert extract_date("25/12/2026", "USD") == "2026-12-25"

    def test_idr_stays_day_first(self):
        assert extract_date("03/04/2026") == "2026-04-03"
        assert extract_date("03/04/2026", "IDR") == "2026-04-03"
        assert extract_date("12/25/2026", "IDR") == "2026-12-25"  # US fallback

    def test_iso_dates_ignore_the_currency(self):
        assert extract_date("2026-07-14", "USD") == extract_date("2026-07-14", "IDR")


class TestParseReceiptCurrency:
    USD_RECEIPT = (
        "BLUE BOTTLE COFFEE\n"
        "03/04/2026 08:15\n"
        "Latte 5.50\n"
        "Subtotal $5.50\n"
        "Tax $0.44\n"
        "Total $5.94\n"
    )
    IDR_RECEIPT = "TOKO MAJU JAYA\n03/04/2026\nTotal Rp 44.000\n"

    def test_a_dollar_receipt(self, categories):
        parsed = parse_receipt(self.USD_RECEIPT, categories)
        assert parsed.currency == "USD"
        assert parsed.amount == "5.94"
        assert parsed.entry_date == "2026-03-04"  # month-first, because it is USD

    def test_a_rupiah_receipt(self, categories):
        parsed = parse_receipt(self.IDR_RECEIPT, categories)
        assert parsed.currency == "IDR"
        assert parsed.amount == "44000"
        assert parsed.entry_date == "2026-04-03"  # day-first, because it is IDR

    def test_the_default_applies_when_nothing_is_printed(self, categories):
        text = "TOKO MAJU\nTotal 44.000"
        assert parse_receipt(text, categories).currency == ""
        assert parse_receipt(text, categories, default_currency="IDR").currency == "IDR"

    def test_a_marker_in_the_text_beats_the_default(self, categories):
        parsed = parse_receipt(self.USD_RECEIPT, categories, default_currency="IDR")
        assert parsed.currency == "USD"

    def test_the_bca_dollar_notification(self, categories):
        text = (
            "Transaksi Kartu Kredit\n"
            "Tanggal : 17 Agustus 2026\n"
            "Merchant : SURFSHARK\n"
            "Sejumlah : USD 56,48\n"
        )
        parsed = parse_receipt(text, categories, default_currency="IDR")
        assert (parsed.currency, parsed.amount) == ("USD", "56.48")
        assert parsed.entry_date == "2026-08-17"
