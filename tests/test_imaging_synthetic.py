"""Smoke tests for the image pipeline on synthetic frames.

The real tuning is measured by OCR recall on private photos (see CLAUDE.md), so
nothing here asserts quality. These only catch the failures a unit can: wrong
shapes, wrong value ranges, a crash on degenerate input, a detector that
stops finding an obvious page.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest

from receiptscanner import imaging
from receiptscanner.config import BUILTIN_DEFAULTS
from receiptscanner.imaging import (
    SourceInfo,
    _order_quad,
    binarize,
    detect_document,
    estimate_skew,
    four_point_transform,
    perspective_matrix,
    rotate,
    scan,
)

OPTIONS = dict(BUILTIN_DEFAULTS["preprocess"])


def text_page(width=600, height=800, lines=14) -> np.ndarray:
    """A white page of horizontal text-like lines, as grayscale."""
    page = np.full((height, width), 255, np.uint8)
    for n in range(lines):
        y = 80 + n * 45
        cv2.putText(
            page, f"Item {n} Total 44.000", (60, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2
        )
    return page


def photo_of_page(page_gray: np.ndarray) -> np.ndarray:
    """A bright page lying on a dark table, as BGR."""
    height, width = page_gray.shape
    frame = np.full((height + 300, width + 300, 3), 55, np.uint8)
    frame[150 : 150 + height, 150 : 150 + width] = cv2.cvtColor(
        page_gray, cv2.COLOR_GRAY2BGR
    )
    return frame


class TestOrderQuad:
    def test_orders_clockwise_from_the_top_left(self):
        shuffled = np.array([[90, 80], [10, 10], [10, 80], [90, 10]], np.float32)
        assert _order_quad(shuffled).tolist() == [
            [10, 10],
            [90, 10],
            [90, 80],
            [10, 80],
        ]

    def test_is_independent_of_the_input_order(self):
        corners = np.array([[10, 10], [90, 10], [90, 80], [10, 80]], np.float32)
        rng = np.random.default_rng(0)
        for _ in range(5):
            assert np.array_equal(
                _order_quad(corners[rng.permutation(4)]), _order_quad(corners)
            )


class TestPerspective:
    QUAD = np.array([[20, 30], [220, 30], [220, 330], [20, 330]], np.float32)

    def test_output_size_follows_the_quad(self):
        _, width, height = perspective_matrix(self.QUAD)
        assert (width, height) == (200, 300)

    def test_four_point_transform_crops_to_the_quad(self):
        frame = np.zeros((400, 400, 3), np.uint8)
        warped = four_point_transform(frame, self.QUAD)
        assert warped.shape[:2] == (300, 200)

    def test_a_degenerate_quad_returns_the_frame_unchanged(self):
        frame = np.zeros((100, 100, 3), np.uint8)
        tiny = np.array([[0, 0], [5, 0], [5, 5], [0, 5]], np.float32)
        assert four_point_transform(frame, tiny) is frame


class TestSkewAndRotate:
    @pytest.mark.parametrize("angle", [4.0, -4.0, 8.0])
    def test_recovers_a_known_rotation(self, angle):
        tilted = rotate(text_page(), angle)
        assert abs(abs(estimate_skew(tilted, 15.0)) - abs(angle)) < 1.5

    def test_a_straight_page_has_no_skew(self):
        assert abs(estimate_skew(text_page(), 15.0)) < 1.0

    def test_angles_beyond_the_limit_are_ignored(self):
        assert estimate_skew(rotate(text_page(), 12.0), 5.0) == 0.0

    def test_a_blank_page_has_no_skew(self):
        assert estimate_skew(np.full((200, 200), 255, np.uint8), 15.0) == 0.0

    def test_rotating_by_zero_keeps_the_size(self):
        page = text_page()
        assert rotate(page, 0.0).shape == page.shape

    def test_rotating_grows_the_canvas_to_fit(self):
        page = text_page()
        assert rotate(page, 20.0).shape[0] > page.shape[0]


class TestBinarize:
    def test_output_is_strictly_black_and_white(self):
        out = binarize(text_page(), OPTIONS)
        assert out.dtype == np.uint8
        assert set(np.unique(out)) <= {0, 255}

    def test_text_comes_out_black_on_a_white_page(self):
        assert float(np.mean(binarize(text_page(), OPTIONS))) > 200

    def test_an_inverted_page_is_flipped_back(self):
        # A photo of a screen: light text on dark. Tesseract needs the reverse.
        inverted = 255 - text_page()
        assert float(np.mean(binarize(inverted, OPTIONS))) > 128  # mostly white


class TestDetectDocument:
    def test_finds_a_bright_page_on_a_dark_table(self):
        frame = photo_of_page(text_page())
        found = detect_document(frame, OPTIONS)

        assert found is not None
        assert 0.2 < found.area_ratio < 0.9
        xs, ys = found.quad[:, 0], found.quad[:, 1]
        # The page sits 150px in from every edge of the frame.
        assert abs(xs.min() - 150) < 40 and abs(ys.min() - 150) < 40
        assert abs(xs.max() - 750) < 40 and abs(ys.max() - 950) < 40
        assert found.mask.shape == frame.shape[:2]

    def test_finds_nothing_in_a_featureless_frame(self):
        flat = np.full((600, 600, 3), 120, np.uint8)
        assert detect_document(flat, OPTIONS) is None

    def test_finds_nothing_in_noise(self):
        rng = np.random.default_rng(1)
        noise = rng.integers(0, 255, (600, 600, 3), dtype=np.uint8)
        assert detect_document(noise, OPTIONS) is None


class TestScan:
    @staticmethod
    def source(frame) -> SourceInfo:
        return SourceInfo(
            path=Path("synthetic.png"),
            width=frame.shape[1],
            height=frame.shape[0],
            size_bytes=1,
            sha256="0" * 64,
        )

    def test_scanning_a_photographed_page(self):
        frame = photo_of_page(text_page())
        result = scan(frame, self.source(frame), OPTIONS)

        assert result.document_detected is True
        assert result.scanned.ndim == 2
        assert set(np.unique(result.scanned)) <= {0, 255}
        assert result.stages  # the audit trail of what ran
        assert result.original_bgr is frame

    def test_scanning_a_frame_with_no_page_still_returns_a_scan(self):
        flat = np.full((400, 400, 3), 120, np.uint8)
        result = scan(flat, self.source(flat), OPTIONS)
        assert result.document_detected is False
        assert result.scanned.ndim == 2

    def test_page_detection_can_be_switched_off(self):
        frame = photo_of_page(text_page())
        result = scan(frame, self.source(frame), {**OPTIONS, "detect_document": False})
        assert result.document_detected is False


def test_write_image_round_trips(tmp_path):
    page = binarize(text_page(), OPTIONS)
    target = tmp_path / "out" / "scan.png"
    target.parent.mkdir()
    imaging.write_image(page, target)
    assert np.array_equal(cv2.imread(str(target), cv2.IMREAD_GRAYSCALE), page)
