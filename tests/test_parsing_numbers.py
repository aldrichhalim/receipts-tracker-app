"""parse_number / format_amount / _money_candidates: the separator rules.

Indonesian receipts write `44.000` for forty-four thousand, English ones write
`10.50`. The parser decides by the digit count after the last separator, so
these cases are the contract that both conventions rely on.
"""

from decimal import Decimal

import pytest

from receiptscanner.parsing import _money_candidates, format_amount, parse_number


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("10.000", "10000"),  # three digits after the separator: thousands
        ("12.345", "12345"),  # ...never a decimal, however it looks
        ("1,234", "1234"),
        ("10.50", "10.50"),  # one or two digits: a decimal fraction
        ("0,5", "0.5"),
        ("1,234.56", "1234.56"),  # English grouping
        ("1.234,56", "1234.56"),  # Indonesian grouping
        ("1.234.567", "1234567"),
        ("1.234.567,89", "1234567.89"),
        ("143,000.00", "143000.00"),
        ("44000", "44000"),
    ],
)
def test_separator_rules(raw, expected):
    assert parse_number(raw) == Decimal(expected)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Rp 44.000", "44000"),
        ("RP 44.000", "44000"),
        ("Rp54.000,00", "54000.00"),
        ("IDR 143,000.00", "143000.00"),
        ("USD 56,48", "56.48"),  # BCA writes dollar amounts with a decimal comma
        ("$ 1,250.00", "1250.00"),
        ("Rp 44.000,-", "44000"),  # trailing ",-" is a printed-amount idiom
    ],
)
def test_currency_symbols_are_stripped(raw, expected):
    assert parse_number(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", None, "abc", "...", ",,", "Rp", "-"])
def test_unparseable_input_is_none(raw):
    assert parse_number(raw) is None


class TestFormatAmount:
    def test_none_is_empty(self):
        assert format_amount(None) == ""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (Decimal("44000"), "44000"),
            (Decimal("44000.00"), "44000"),  # whole amounts print without cents
            (44000.0, "44000"),
            (Decimal("12.5"), "12.50"),
            (Decimal("12.50"), "12.50"),
            (12.5, "12.50"),
        ],
    )
    def test_formatting(self, value, expected):
        assert format_amount(value) == expected


class TestMoneyCandidates:
    def test_every_number_on_the_line_by_default(self):
        assert _money_candidates("2 x 15.000 = 30.000") == [
            Decimal("2"),
            Decimal("15000"),
            Decimal("30000"),
        ]

    def test_zero_and_negatives_never_qualify(self):
        assert _money_candidates("Qty 0 Disc 0,00") == []

    def test_long_unformatted_runs_are_identifiers_not_money(self):
        # Card numbers, merchant IDs and approval codes dwarf any real total.
        assert _money_candidates("4111111111111111", money_shaped_only=True) == []
        assert _money_candidates("1234567", money_shaped_only=True) == []

    def test_short_digit_runs_and_grouped_numbers_are_money(self):
        assert _money_candidates("123456", money_shaped_only=True) == [
            Decimal("123456")
        ]
        assert _money_candidates("1.234.567", money_shaped_only=True) == [
            Decimal("1234567")
        ]

    def test_more_than_twelve_digits_is_rejected_even_when_grouped(self):
        assert _money_candidates("1.234.567.890.123", money_shaped_only=True) == []
