"""The pure parts of ocr.py: rebuilding text and confidence from word boxes.

These take the dict that pytesseract's image_to_data returns, so they can be
checked without Tesseract being installed.
"""

import pytest

from receiptscanner.ocr import _mean_confidence, _rebuild_text, _word_count


def tess(*words):
    """(text, conf, block, par, line, word) tuples -> an image_to_data dict."""
    data = {
        key: []
        for key in (
            "text",
            "conf",
            "page_num",
            "block_num",
            "par_num",
            "line_num",
            "word_num",
        )
    }
    for text, conf, block, par, line, word in words:
        data["text"].append(text)
        data["conf"].append(conf)
        data["page_num"].append(1)
        data["block_num"].append(block)
        data["par_num"].append(par)
        data["line_num"].append(line)
        data["word_num"].append(word)
    return data


class TestRebuildText:
    def test_words_on_a_line_are_joined_and_lines_are_separated(self):
        data = tess(
            ("Total", 96, 1, 1, 1, 1),
            ("Paid", 95, 1, 1, 1, 2),
            ("44.000", 90, 1, 1, 2, 1),
        )
        assert _rebuild_text(data) == "Total Paid\n44.000"

    def test_words_are_ordered_by_word_number_not_arrival(self):
        data = tess(("Paid", 95, 1, 1, 1, 2), ("Total", 96, 1, 1, 1, 1))
        assert _rebuild_text(data) == "Total Paid"

    def test_lines_are_ordered_by_block_paragraph_then_line(self):
        data = tess(
            ("later", 90, 2, 1, 1, 1),
            ("second", 90, 1, 1, 2, 1),
            ("first", 90, 1, 1, 1, 1),
        )
        assert _rebuild_text(data) == "first\nsecond\nlater"

    def test_blank_and_whitespace_words_are_dropped(self):
        data = tess(
            ("", -1, 1, 1, 1, 0), ("  ", -1, 1, 1, 1, 1), ("Total", 90, 1, 1, 1, 2)
        )
        assert _rebuild_text(data) == "Total"

    def test_empty(self):
        assert _rebuild_text(tess()) == ""
        assert _rebuild_text({}) == ""


class TestMeanConfidence:
    def test_average_of_real_words(self):
        data = tess(("a", 90, 1, 1, 1, 1), ("b", 80, 1, 1, 1, 2))
        assert _mean_confidence(data) == 85.0

    def test_layout_rows_with_negative_confidence_are_ignored(self):
        data = tess(("", -1, 1, 1, 0, 0), ("a", 90, 1, 1, 1, 1))
        assert _mean_confidence(data) == 90.0

    def test_blank_words_do_not_count(self):
        data = tess(("a", 90, 1, 1, 1, 1), ("", 0, 1, 1, 1, 2))
        assert _mean_confidence(data) == 90.0

    def test_a_word_with_zero_confidence_does_count(self):
        data = tess(("a", 90, 1, 1, 1, 1), ("b", 0, 1, 1, 1, 2))
        assert _mean_confidence(data) == 45.0

    def test_rounded_to_two_places(self):
        data = tess(("a", 90, 1, 1, 1, 1), ("b", 91, 1, 1, 1, 2), ("c", 91, 1, 1, 1, 3))
        assert _mean_confidence(data) == 90.67

    def test_string_confidences_are_accepted(self):
        data = tess(("a", "90", 1, 1, 1, 1))
        assert _mean_confidence(data) == 90.0

    @pytest.mark.parametrize("data", [tess(), {}])
    def test_no_words_is_zero(self, data):
        assert _mean_confidence(data) == 0.0


class TestWordCount:
    def test_counts_only_non_blank_words(self):
        data = tess(("a", 90, 1, 1, 1, 1), ("", -1, 1, 1, 1, 2), (" ", -1, 1, 1, 1, 3))
        assert _word_count(data) == 1

    def test_empty(self):
        assert _word_count({}) == 0
