"""pipeline.py: how a processed receipt becomes a database record."""

from pathlib import Path

import numpy as np
import pytest

from helpers import build_mbox, make_message
from receiptscanner import pipeline
from receiptscanner.imaging import ScanResult, SourceInfo
from receiptscanner.mail import EmailReceipt, iter_mbox
from receiptscanner.ocr import OcrResult
from receiptscanner.parsing import ParsedReceipt
from receiptscanner.pipeline import PipelineOutput, build_output_path

SHA = "ab" * 32


def output(
    *, email=None, with_scan=True, rendered=False, **suggestion
) -> PipelineOutput:
    scan = None
    if with_scan:
        scan = ScanResult(
            original_bgr=np.zeros((10, 10, 3), np.uint8),
            scanned=np.zeros((20, 30), np.uint8),
            source=SourceInfo(Path("a.jpg"), 30, 20, 1234, SHA),
            document_detected=True,
            deskew_angle=1.5,
        )
    return PipelineOutput(
        ocr=OcrResult(
            text="Total 44.000", mean_confidence=91.0, word_count=2, lang="ind"
        ),
        suggestion=ParsedReceipt(**{"amount": "44000", **suggestion}),
        scan=scan,
        scanned_path=Path("/tmp/s.png") if with_scan else None,
        email=email,
        source_path=Path("/tmp/a.jpg"),
        rendered=rendered,
    )


def email():
    return EmailReceipt(
        index=0,
        mbox_path=Path("in.mbox"),
        subject="Your receipt",
        sender_name="Grab",
        sender_email="no-reply@grab.com",
        message_id="<m@grab>",
    )


class TestSourceKind:
    @pytest.mark.parametrize(
        "kwargs, expected",
        [
            ({}, "image"),
            ({"email": email()}, "email_image"),
            ({"email": email(), "rendered": True}, "email_render"),
            ({"email": email(), "with_scan": False}, "email"),
        ],
    )
    def test_the_provenance_is_derived_from_how_the_receipt_arrived(
        self, config, kwargs, expected
    ):
        assert output(**kwargs).as_record(config)["source_kind"] == expected


class TestAsRecord:
    def test_ocr_fields(self, config):
        record = output().as_record(config)
        assert record["ocr_text_raw"] == "Total 44.000"
        assert (
            record["ocr_lang"],
            record["ocr_confidence"],
            record["ocr_word_count"],
        ) == (
            "ind",
            91.0,
            2,
        )

    def test_image_details_come_from_the_scan(self, config):
        record = output().as_record(config)
        assert (record["scanned_width"], record["scanned_height"]) == (30, 20)
        assert record["source_sha256"] == SHA
        assert record["document_detected"] == 1
        assert record["deskew_angle"] == 1.5

    def test_email_provenance_is_attached(self, config):
        record = output(email=email(), rendered=True).as_record(config)
        assert record["email_message_id"] == "<m@grab>"
        assert record["email_subject"] == "Your receipt"
        assert record["email_from"] == "Grab <no-reply@grab.com>"

    def test_a_plain_photo_has_no_email_columns(self, config):
        assert "email_message_id" not in output().as_record(config)

    def test_a_record_only_carries_columns_the_store_knows(self, config):
        from receiptscanner.db import COLUMNS

        record = output(email=email(), rendered=True).as_record(config)
        assert set(record) <= set(COLUMNS)

    def test_the_record_can_be_saved(self, config, store):
        record = output(email=email(), rendered=True).as_record(config)
        record.update(entry_date="2026-07-14", name="Grab", amount=44000.0)
        store.save(record)
        assert store.find_by_message_id("<m@grab>")["source_kind"] == "email_render"

    def test_has_image(self):
        assert output().has_image is True
        assert output(with_scan=False).has_image is False


class TestBuildOutputPath:
    def test_follows_the_filename_template(self, config):
        path = build_output_path(Path("/x/IMG_5672.jpeg"), SHA, config)
        assert path.parent == config.output_dir
        assert path.name == "IMG_5672_abababab.png"

    def test_same_name_different_photo_keeps_both(self, config):
        config.output_dir.mkdir(parents=True)
        first = build_output_path(Path("/x/IMG.jpeg"), SHA, config)
        first.write_bytes(b"x")
        second = build_output_path(Path("/x/IMG.jpeg"), SHA, config)
        second.write_bytes(b"x")
        third = build_output_path(Path("/x/IMG.jpeg"), SHA, config)

        assert len({first, second, third}) == 3
        assert second.name == "IMG_abababab-1.png"
        assert third.name == "IMG_abababab-2.png"

    def test_unsafe_characters_are_cleaned(self, config):
        path = build_output_path(Path("/x/my receipt (1)!.jpeg"), SHA, config)
        assert " " not in path.name and "(" not in path.name and "!" not in path.name

    def test_a_template_without_an_extension_gets_png(self, config):
        config._data["scan_filename_template"] = "{stem}"
        assert build_output_path(Path("/x/a.jpeg"), SHA, config).suffix == ".png"


@pytest.mark.ocr
class TestProcessEmailEndToEnd:
    """Render -> Tesseract -> parse, on a synthetic message (needs Tesseract)."""

    HTML = (
        "<table>"
        "<tr><td>Fare</td><td>33.500</td></tr>"
        "<tr><td>Platform Fee</td><td>10.500</td></tr>"
        "<tr><td>Total Paid</td><td>44.000</td></tr>"
        "</table>"
    )

    @pytest.fixture
    def processed(self, tmp_path, config):
        box = build_mbox(
            tmp_path / "in.mbox",
            [
                make_message(
                    subject="Your receipt", sender="Grab <g@grab.com>", html=self.HTML
                )
            ],
        )
        (item,) = iter_mbox(box, tmp_path / "att")
        return item, pipeline.process_email(item, config)

    def test_the_total_survives_the_round_trip(self, processed):
        _, out = processed
        assert out.suggestion.amount == "44000"

    def test_the_fallback_date_comes_from_the_message_header(self, processed):
        _, out = processed
        assert out.suggestion.entry_date == "2026-07-14"

    def test_the_merchant_comes_from_the_message_not_from_ocr(self, processed):
        item, out = processed
        assert out.suggestion.name == item.merchant_guess()

    def test_it_is_marked_as_rendered_and_has_an_image_on_disk(self, processed):
        _, out = processed
        assert out.rendered is True and out.has_image
        assert out.scanned_path.is_file()

    def test_nothing_is_written_outside_the_sandbox(self, processed, config):
        _, out = processed
        assert config.output_dir in out.scanned_path.parents

    def test_the_record_is_saveable_and_tagged(self, processed, config, store):
        item, out = processed
        record = out.as_record(config)
        record.update(entry_date="2026-07-14", name="Grab", amount=44000.0)
        store.save(record)
        assert (
            store.find_by_message_id(item.message_id)["source_kind"] == "email_render"
        )
