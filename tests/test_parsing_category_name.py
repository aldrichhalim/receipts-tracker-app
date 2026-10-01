"""guess_category, extract_name and the composed parse_receipt."""

from decimal import Decimal

import pytest

from receiptscanner.parsing import (
    ParsedReceipt,
    extract_name,
    guess_category,
    parse_receipt,
)


class TestGuessCategory:
    def test_obvious_keyword(self, categories):
        assert guess_category("KFC Sudirman\nChicken Bucket", categories) == (
            "Makanan & Minuman"
        )

    def test_a_ride_picked_up_outside_a_kfc_stays_transport(self, categories):
        # Specificity wins: "grab" scores higher than the incidental "kfc".
        text = "Grab Car Standard\nPicked up at KFC Juanda Jakarta Pusat"
        assert guess_category(text, categories) == "Transportasi"

    def test_grabfood_outweighs_the_bare_grab_it_contains(self, categories):
        assert guess_category("GrabFood order\nNonna Pasta", categories) == (
            "Makanan & Minuman"
        )

    def test_unknown_text_falls_back(self, categories):
        assert guess_category("zzz qqq", categories) == "Lainnya"

    def test_empty_text_falls_back(self, categories):
        assert guess_category("", categories) == "Lainnya"

    def test_a_category_the_user_removed_is_never_returned(self):
        assert guess_category("KFC", ["Lainnya", "Transportasi"]) == "Lainnya"

    def test_without_the_fallback_category_the_last_allowed_one_is_used(self):
        assert guess_category("zzz", ["A", "B"]) == "B"

    def test_no_allowed_categories_is_empty(self):
        assert guess_category("KFC", []) == ""

    def test_case_insensitive(self, categories):
        assert guess_category("kfc", categories) == guess_category("KFC", categories)


class TestExtractName:
    def test_first_substantial_header_line(self):
        text = "TOKO MAJU JAYA\nJl. Mangga No 5\nTotal 10.000"
        assert extract_name(text) == "TOKO MAJU JAYA"

    def test_address_phone_and_barcode_lines_are_skipped(self):
        text = "Jl. Raya Kebon Jeruk 12\n021-5551234\n8991234567890\nBAKSO PAK DE"
        assert extract_name(text) == "BAKSO PAK DE"

    def test_surrounding_punctuation_is_trimmed(self):
        assert extract_name("**** WARUNG SEDERHANA ****") == "WARUNG SEDERHANA"

    def test_garbage_has_no_name(self):
        assert extract_name("12345\n--\n:::\n.,") is None

    def test_empty_has_no_name(self):
        assert extract_name("") is None

    def test_only_the_header_block_is_considered(self):
        filler = "\n".join(["1"] * 12)
        assert extract_name(f"{filler}\nTOKO TERLAMBAT") is None

    def test_name_is_capped_in_length(self):
        assert len(extract_name("A" * 300 + " " + "B" * 20)) <= 120


class TestParseReceipt:
    RECEIPT = (
        "TOKO MAJU JAYA\n"
        "Jl. Mangga No 5\n"
        "16/06/2026 14:30\n"
        "Nasi Goreng 2 x 20.000 40.000\n"
        "Subtotal 40.000\n"
        "Total 44.000\n"
        "Tunai 50.000\n"
        "Kembali 6.000\n"
    )

    def test_composes_every_field(self, categories):
        parsed = parse_receipt(self.RECEIPT, categories)
        assert parsed.entry_date == "2026-06-16"
        assert parsed.name == "TOKO MAJU JAYA"
        assert parsed.amount == "44000"
        assert parsed.category in categories

    def test_returns_strings_for_the_review_form(self, categories):
        parsed = parse_receipt(self.RECEIPT, categories)
        assert all(
            isinstance(value, str)
            for value in (
                parsed.entry_date,
                parsed.category,
                parsed.name,
                parsed.amount,
            )
        )

    def test_fallback_date_is_used_when_the_text_has_none(self, categories):
        parsed = parse_receipt("TOKO MAJU\nTotal 44.000", categories, "2026-01-02")
        assert parsed.entry_date == "2026-01-02"

    def test_a_date_in_the_text_beats_the_fallback(self, categories):
        parsed = parse_receipt(self.RECEIPT, categories, "2026-01-02")
        assert parsed.entry_date == "2026-06-16"

    @pytest.mark.parametrize("text", ["", "   \n\t "])
    def test_empty_text_still_yields_a_usable_suggestion(self, text, categories):
        parsed = parse_receipt(text, categories, "2026-01-02")
        assert parsed == ParsedReceipt(
            entry_date="2026-01-02", category="Lainnya", name="", amount=""
        )

    def test_as_dict_matches_the_fields(self, categories):
        parsed = parse_receipt(self.RECEIPT, categories)
        assert parsed.as_dict() == {
            "entry_date": parsed.entry_date,
            "category": parsed.category,
            "name": parsed.name,
            "amount": parsed.amount,
        }

    def test_amount_round_trips_through_decimal(self, categories):
        parsed = parse_receipt(self.RECEIPT, categories)
        assert Decimal(parsed.amount) == Decimal("44000")
