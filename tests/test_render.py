"""render.py: drawing an e-receipt's markup as an image for OCR."""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from receiptscanner import render
from receiptscanner.mail import EmailReceipt
from receiptscanner.render import (
    DEFAULTS,
    RenderError,
    _dedupe,
    _layout,
    _sanitize,
    _wrap,
    render_email,
)

TABLE = (
    "<table>"
    "<tr><td>Fare</td><td>33.500</td></tr>"
    "<tr><td>Platform Fee</td><td>10.500</td></tr>"
    "<tr><td>Total Paid</td><td>44.000</td></tr>"
    "</table>"
)


def receipt(**kw):
    return EmailReceipt(index=0, mbox_path=Path("x.mbox"), **kw)


class TestSanitize:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Total ☕ 1.000", "Total 1.000"),  # dingbat
            ("Rated ★★★★★", "Rated"),  # geometric shapes
            ("Go → next", "Go next"),  # arrow
            ("Hi 😀 there", "Hi there"),  # emoji
            ("a​b", "a b"),  # zero-width space
            ("icon  label", "icon label"),  # private-use icon font
            ("  too   many   spaces  ", "too many spaces"),
        ],
    )
    def test_characters_the_font_cannot_draw_are_removed(self, raw, expected):
        # Pillow draws these as tofu boxes, which OCR reads as invented words.
        assert _sanitize(raw) == expected

    @pytest.mark.parametrize("text", ["Nasi Goreng", "Café", "Rp 44.000,-", "日本語"])
    def test_ordinary_text_is_untouched(self, text):
        assert _sanitize(text) == text


class TestDedupe:
    def test_consecutive_duplicates_collapse(self):
        rows = [["a"], ["a"], ["a"], ["b"]]
        assert _dedupe(rows) == [["a"], ["b"]]

    def test_separated_duplicates_are_kept(self):
        assert _dedupe([["a"], ["b"], ["a"]]) == [["a"], ["b"], ["a"]]

    def test_empty(self):
        assert _dedupe([]) == []


class TestWrap:
    @pytest.fixture
    def draw(self):
        return ImageDraw.Draw(Image.new("L", (1, 1)))

    def test_short_text_is_one_line(self, draw):
        font = render._load_font("", 20)
        assert _wrap(draw, "Total Paid", font, 5000) == ["Total Paid"]

    def test_long_text_wraps_and_never_exceeds_the_limit(self, draw):
        font = render._load_font("", 20)
        text = " ".join(["Terima kasih atas kunjungan Anda"] * 6)
        lines = _wrap(draw, text, font, 300)
        assert len(lines) > 1
        assert " ".join(lines) == text  # no word lost or split
        assert all(draw.textlength(line, font=font) <= 300 for line in lines)

    def test_a_single_overlong_word_is_not_split(self, draw):
        font = render._load_font("", 20)
        assert _wrap(draw, "Supercalifragilisticexpialidocious", font, 20) == [
            "Supercalifragilisticexpialidocious"
        ]

    def test_empty_text_has_no_lines(self, draw):
        assert _wrap(draw, "", render._load_font("", 20), 300) == []


class TestLayout:
    """The measure pass: every string becomes an (x, y, text, font) placement."""

    @pytest.fixture
    def parts(self):
        draw = ImageDraw.Draw(Image.new("L", (1, 1)))
        font = render._load_font("", DEFAULTS["font_size"])
        header_font = render._load_font("", DEFAULTS["header_font_size"])
        return draw, font, header_font

    def place(self, parts, rows, header=()):
        draw, font, header_font = parts
        return _layout(draw, rows, header, font, header_font, dict(DEFAULTS))

    def test_a_two_cell_row_is_label_left_value_right_on_one_baseline(self, parts):
        draw, font, _ = parts
        placements = self.place(parts, [["Total Paid", "44.000"]])
        by_text = {text: (x, y) for x, y, text, _ in placements}

        label_x, label_y = by_text["Total Paid"]
        value_x, value_y = by_text["44.000"]
        value_width = int(draw.textlength("44.000", font=font))

        assert label_x == DEFAULTS["margin"]
        assert value_x == DEFAULTS["width"] - DEFAULTS["margin"] - value_width
        assert label_y == value_y

    def test_a_single_cell_starts_at_the_margin(self, parts):
        ((x, _, text, _),) = self.place(parts, [["Thank you"]])
        assert (x, text) == (DEFAULTS["margin"], "Thank you")

    def test_three_cells_become_left_to_right_columns(self, parts):
        placements = self.place(parts, [["a", "b", "c"]])
        xs = [x for x, _, _, _ in placements]
        assert xs == sorted(xs) and len(set(xs)) == 3
        assert len({y for _, y, _, _ in placements}) == 1

    def test_rows_advance_downward(self, parts):
        placements = self.place(parts, [["one"], ["two"], ["three"]])
        ys = [y for _, y, _, _ in placements]
        assert ys == sorted(ys) and len(set(ys)) == 3

    def test_the_header_is_drawn_first_in_the_larger_face(self, parts):
        _, font, header_font = parts
        placements = self.place(parts, [["body"]], header=["Subject line"])
        assert placements[0][2] == "Subject line"
        assert placements[0][3] is header_font
        assert placements[1][3] is font
        assert placements[1][1] > placements[0][1]


class TestRenderEmail:
    OPTIONS = {"width": 800}

    def test_returns_a_grayscale_array_of_the_requested_width(self):
        image = render_email(receipt(body_html=TABLE), self.OPTIONS)
        assert image.ndim == 2 and image.dtype == np.uint8
        assert image.shape[1] == 800

    def test_is_black_text_on_a_white_page(self):
        image = render_email(receipt(body_html=TABLE), self.OPTIONS)
        assert image.max() == 255 and image.min() == 0
        assert (image == 255).mean() > 0.8  # mostly page, not ink

    def test_more_content_makes_a_taller_page(self):
        short = render_email(receipt(body_html=TABLE), self.OPTIONS)
        rows = "".join(
            f"<tr><td>Item {i}</td><td>{i}.000</td></tr>" for i in range(1, 40)
        )
        tall = render_email(receipt(body_html=f"<table>{rows}</table>"), self.OPTIONS)
        assert tall.shape[0] > short.shape[0]

    def test_height_is_clamped(self):
        rows = "".join(
            f"<tr><td>Item {i}</td><td>{i}.000</td></tr>" for i in range(1, 400)
        )
        image = render_email(
            receipt(body_html=f"<table>{rows}</table>"),
            {"width": 800, "max_height": 500},
        )
        assert image.shape[0] == 500

    def test_a_message_with_only_plain_text_is_still_drawn(self):
        image = render_email(
            receipt(body_text="Total 44.000\nTerima kasih"), self.OPTIONS
        )
        assert image.min() == 0

    def test_the_header_alone_is_enough_to_draw(self):
        image = render_email(receipt(subject="Your receipt"), self.OPTIONS)
        assert image.min() == 0

    def test_an_empty_message_cannot_be_rendered(self):
        with pytest.raises(RenderError):
            render_email(receipt(), self.OPTIONS)

    def test_a_message_of_only_undrawable_characters_cannot_be_rendered(self):
        with pytest.raises(RenderError):
            render_email(receipt(body_text="😀 ☕ ★"), self.OPTIONS)

    def test_partial_options_fall_back_to_the_defaults(self):
        image = render_email(receipt(body_html=TABLE), {})
        assert image.shape[1] == DEFAULTS["width"]

    def test_identical_input_renders_identically(self):
        a = render_email(receipt(body_html=TABLE), self.OPTIONS)
        b = render_email(receipt(body_html=TABLE), self.OPTIONS)
        assert np.array_equal(a, b)
