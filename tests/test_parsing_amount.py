"""extract_amount: which number on the receipt is the total.

Each case is a rule that CLAUDE.md documents as load-bearing. They exist so a
refactor cannot quietly trade one back for a simpler-looking alternative.
"""

from decimal import Decimal

import pytest

from receiptscanner.parsing import extract_amount


def amount(text):
    return extract_amount(text)


class TestKeywordTiers:
    def test_total_beats_subtotal_even_when_smaller(self):
        # "subtotal" contains "total": first-match would rank it as the total.
        assert amount("Subtotal 60.000\nTotal 55.000") == Decimal("55000")

    def test_order_of_the_lines_does_not_matter(self):
        assert amount("Total 55.000\nSubtotal 60.000") == Decimal("55000")

    def test_grand_total_beats_plain_total(self):
        assert amount("Total 90.000\nGrand Total 80.000") == Decimal("80000")

    def test_within_a_tier_the_larger_amount_wins(self):
        assert amount("Total 10.000\nTotal 20.000") == Decimal("20000")

    def test_indonesian_labels(self):
        assert amount("TOTAL BAYAR 125.500\nTUNAI 150.000") == Decimal("125500")

    def test_sejumlah_matches_the_jumlah_tier(self):
        # BCA card notifications label the amount "Sejumlah".
        assert amount("Sejumlah : Rp54.000,00") == Decimal("54000")

    def test_cash_tendered_never_outranks_the_total(self):
        assert amount("Total 50.000\nTunai 100.000") == Decimal("50000")


class TestExcludedLines:
    @pytest.mark.parametrize(
        "line",
        [
            "Kembali 100.000",
            "Change 100.000",
            "PPN 11.000",
            "Tax 11.000",
            "Diskon 70.000",
            "Service charge 70.000",
            "Poin 700.000",
            "Kasir 900.000",
        ],
    )
    def test_lines_that_are_never_the_total_are_skipped(self, line):
        assert amount(f"Total 50.000\n{line}") == Decimal("50000")


class TestIdentifiersAreNotTotals:
    def test_card_number_loses_to_the_real_total(self):
        assert amount("4111111111111111\nTotal 25.000") == Decimal("25000")

    def test_long_reference_number_loses_to_the_real_total(self):
        assert amount("Ref 1234567890\nTotal 25.000") == Decimal("25000")

    def test_an_identifier_sharing_the_total_line_does_not_win(self):
        # Without the money-shaped filter max() picks the reference number.
        assert amount("Total 25.000 ref 1234567890123") == Decimal("25000")

    def test_with_no_label_at_all_a_card_number_still_loses(self):
        # The fallback takes the largest candidate, so the filter is all that
        # stands between a 16-digit card number and the amount field.
        assert amount("4111111111111111\n45.000") == Decimal("45000")


class TestAmountFloor:
    """Values under 100 are quantities and item counts, never an IDR total."""

    @pytest.mark.parametrize("text", ["Total 50", "Total 99", "Total 3"])
    def test_below_the_floor_is_ignored(self, text):
        assert amount(text) is None

    def test_the_floor_itself_qualifies(self):
        assert amount("Total 100") == Decimal("100")


class TestFallbacks:
    @pytest.mark.parametrize("text", ["", "   \n  ", "Terima kasih", "Thank you"])
    def test_nothing_to_find(self, text):
        assert amount(text) is None

    def test_no_labels_at_all_takes_the_largest_money_shaped_number(self):
        assert amount("Terima kasih\n20.000\n45.000") == Decimal("45000")

    def test_slightly_larger_unlabelled_amount_overrides_a_mangled_label(self):
        # "Grand Total" read as "Irand Total", while the payment line survived.
        assert amount("Total 40.000\n55.000") == Decimal("55000")

    def test_the_override_is_bounded_at_one_and_a_half_times(self):
        assert amount("Total 40.000\n70.000") == Decimal("40000")

    def test_nbsp_inside_a_number_line_is_normalised(self):
        assert amount("Total 44.000") == Decimal("44000")


class TestKnownBugs:
    """Existing defects, recorded rather than frozen. Each is a strict xfail, so
    fixing the bug turns it into an unexpected pass and forces the marker off."""

    OVERRIDE = (
        "KNOWN BUG, the unlabelled-override heuristic: an item price up to 1.5x "
        "the labelled total replaces it, so a discounted receipt reports the "
        "item price. It exists to rescue a mangled 'Grand Total' label, so any "
        "change needs an OCR-recall measurement against data/, not a unit edit."
    )

    @pytest.mark.xfail(strict=True, reason=OVERRIDE)
    def test_a_discounted_item_price_does_not_replace_the_total(self):
        text = "Nasi Goreng 50.000\nDiskon 10.000\nTotal 45.000"
        assert amount(text) == Decimal("45000")
