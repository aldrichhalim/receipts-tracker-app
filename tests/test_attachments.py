"""The receipt-attachment PDF (attachments.py has no Tk dependency by design).

Everything is synthetic and runs under the isolated HOME from conftest.py. The
PDF is inspected as bytes, because the only thing that matters is what a reader
would open: how many pages, what size, what metadata.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from helpers import build_mbox, make_message, make_record
from receiptscanner import attachments, report
from receiptscanner.attachments import (
    AttachmentError,
    Cancelled,
    MailboxCache,
    expected_origin,
    resolve_image,
    write_pdf,
)
from receiptscanner.db import ReceiptStore

RECEIPT_HTML = (
    "<table>"
    "<tr><td>Grab ride</td><td>Rp 44.000</td></tr>"
    "<tr><td>Fee</td><td>Rp 1.000</td></tr>"
    "<tr><td>Total</td><td>Rp 45.000</td></tr>"
    "</table>"
)


# -- builders -----------------------------------------------------------------
def scan_png(path: Path, size=(1348, 900), text="receipt") -> Path:
    """A black-on-white scan like the pipeline writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("L", size, 255)
    ImageDraw.Draw(image).text((40, 40), text, fill=0)
    image.save(path)
    return path


def mailbox(tmp_path: Path, *message_ids: str) -> Path:
    messages = [
        make_message(subject=f"Receipt {i}", html=RECEIPT_HTML, message_id=message_id)
        for i, message_id in enumerate(message_ids)
    ]
    (tmp_path / "mail").mkdir(exist_ok=True)
    return build_mbox(tmp_path / "mail" / "Unread.mbox", messages)


def row(i: int = 1, **overrides):
    values = {
        "id": i,
        "entry_date": "2026-07-14",
        "category": "Transportasi",
        "name": f"Shop {i}",
        "amount": 44000.0,
        "currency": "IDR",
        "source_kind": "email_render",
        "scanned_path": None,
        "source_path": None,
        "email_message_id": None,
        "ocr_text": "stored text",
    }
    values.update(overrides)
    return values


def page_count(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf))


def index_pages_for(entries: int) -> int:
    return max(1, math.ceil(entries / attachments.INDEX_ROWS_PER_PAGE))


@pytest.fixture
def out(tmp_path) -> Path:
    return tmp_path / "out" / "report.pdf"


# -- finding each entry's picture --------------------------------------------
class TestResolveImage:
    def test_a_stored_scan_is_used_as_is(self, tmp_path, config):
        path = scan_png(tmp_path / "scans" / "a.png", size=(1348, 700))
        with MailboxCache() as mailboxes:
            source = resolve_image(row(scanned_path=str(path)), config, mailboxes)
        assert source.origin == "scan"
        assert source.image.size == (1348, 700)

    def test_a_missing_scan_is_redrawn_from_its_mailbox(self, tmp_path, config):
        box = mailbox(tmp_path, "<a@x>", "<b@x>")
        legacy = row(
            source_kind="email",
            scanned_path=None,
            source_path=str(box),
            email_message_id="<b@x>",
        )
        with MailboxCache() as mailboxes:
            source = resolve_image(legacy, config, mailboxes)
        assert source.origin == "redrawn"
        assert source.image.width == config.email_render["width"]
        assert source.image.height > 100

    def test_a_scan_that_vanished_falls_back_to_the_mailbox(self, tmp_path, config):
        box = mailbox(tmp_path, "<a@x>")
        gone = row(
            scanned_path=str(tmp_path / "scans" / "deleted.png"),
            source_path=str(box),
            email_message_id="<a@x>",
        )
        with MailboxCache() as mailboxes:
            assert resolve_image(gone, config, mailboxes).origin == "redrawn"

    def test_an_unreadable_scan_falls_back_instead_of_failing(self, tmp_path, config):
        broken = tmp_path / "scans" / "broken.png"
        broken.parent.mkdir()
        broken.write_bytes(b"this is not a png")
        box = mailbox(tmp_path, "<a@x>")
        legacy = row(
            scanned_path=str(broken), source_path=str(box), email_message_id="<a@x>"
        )
        with MailboxCache() as mailboxes:
            assert resolve_image(legacy, config, mailboxes).origin == "redrawn"

    def test_a_photo_without_a_scan_uses_the_original(self, tmp_path, config):
        photo = tmp_path / "photos" / "IMG_1.png"
        photo.parent.mkdir()
        Image.new("RGB", (800, 1000), (240, 240, 240)).save(photo)
        lost = row(source_kind="image", scanned_path=None, source_path=str(photo))
        with MailboxCache() as mailboxes:
            source = resolve_image(lost, config, mailboxes)
        assert source.origin == "original"
        assert source.image.size == (800, 1000)

    def test_nothing_on_disk_gives_a_placeholder_not_a_failure(self, config):
        with MailboxCache() as mailboxes:
            source = resolve_image(row(source_kind="image"), config, mailboxes)
        assert (source.origin, source.image) == ("none", None)

    def test_a_message_id_the_mailbox_does_not_hold_is_not_found(
        self, tmp_path, config
    ):
        box = mailbox(tmp_path, "<a@x>")
        stranger = row(
            source_kind="email", source_path=str(box), email_message_id="<zzz@x>"
        )
        with MailboxCache() as mailboxes:
            assert resolve_image(stranger, config, mailboxes).origin == "none"

    def test_a_missing_mailbox_is_not_found_rather_than_an_error(
        self, tmp_path, config
    ):
        orphan = row(
            source_kind="email",
            source_path=str(tmp_path / "gone.mbox"),
            email_message_id="<a@x>",
        )
        with MailboxCache() as mailboxes:
            assert resolve_image(orphan, config, mailboxes).origin == "none"

    def test_a_photo_taken_from_an_email_is_not_mistaken_for_a_mailbox(
        self, tmp_path, config
    ):
        # email_image rows keep the extracted attachment in source_path, which is
        # a picture, not a mailbox.
        attachment = tmp_path / "email_attachments" / "p.png"
        attachment.parent.mkdir()
        Image.new("L", (600, 600), 255).save(attachment)
        row_ = row(
            source_kind="email_image",
            scanned_path=None,
            source_path=str(attachment),
            email_message_id="<a@x>",
        )
        with MailboxCache() as mailboxes:
            assert resolve_image(row_, config, mailboxes).origin == "original"

    def test_a_mailbox_is_read_once_however_many_entries_use_it(
        self, tmp_path, config, monkeypatch
    ):
        from receiptscanner import mail

        box = mailbox(tmp_path, *(f"<m{i}@x>" for i in range(5)))
        calls = []
        real = mail.iter_mbox
        monkeypatch.setattr(
            mail, "iter_mbox", lambda *a, **k: calls.append(a[0]) or real(*a, **k)
        )
        with MailboxCache() as mailboxes:
            for i in range(5):
                legacy = row(
                    i,
                    source_kind="email",
                    source_path=str(box),
                    email_message_id=f"<m{i}@x>",
                )
                assert resolve_image(legacy, config, mailboxes).origin == "redrawn"
        assert len(calls) == 1

    def test_extracted_attachments_never_land_beside_the_mailbox(
        self, tmp_path, config
    ):
        box = mailbox(tmp_path, "<a@x>")
        before = sorted(p.name for p in box.parent.rglob("*"))
        with MailboxCache() as mailboxes:
            resolve_image(
                row(
                    source_kind="email", source_path=str(box), email_message_id="<a@x>"
                ),
                config,
                mailboxes,
            )
        assert sorted(p.name for p in box.parent.rglob("*")) == before


class TestExpectedOrigin:
    """The dialog's preview must agree with what the export will do."""

    def test_it_matches_resolve_image_for_every_case(self, tmp_path, config):
        box = mailbox(tmp_path, "<a@x>")
        photo = tmp_path / "IMG.png"
        Image.new("RGB", (300, 300), 255).save(photo)
        cases = [
            row(scanned_path=str(scan_png(tmp_path / "s.png"))),
            row(source_kind="email", source_path=str(box), email_message_id="<a@x>"),
            row(source_kind="image", source_path=str(photo)),
            row(source_kind="image"),
        ]
        with MailboxCache() as mailboxes:
            for case in cases:
                assert (
                    expected_origin(case)
                    == resolve_image(case, config, mailboxes).origin
                )


# -- the PDF -------------------------------------------------------------------
class TestWritePdf:
    def entries(self, tmp_path, n=3, **overrides):
        return [
            row(
                i,
                scanned_path=str(scan_png(tmp_path / "scans" / f"{i}.png")),
                **overrides,
            )
            for i in range(1, n + 1)
        ]

    def test_it_writes_an_index_and_one_page_per_entry(self, tmp_path, config, out):
        rows = self.entries(tmp_path, 3)
        result = write_pdf(rows, out, config, start="2026-07-01", end="2026-07-31")
        pdf = out.read_bytes()
        assert pdf.startswith(b"%PDF")
        assert result.entries == 3
        assert result.pages == index_pages_for(3) + 3
        assert page_count(pdf) == result.pages

    def test_pages_are_a4_at_200_dpi(self, tmp_path, config, out):
        write_pdf(self.entries(tmp_path, 1), out, config, start="a", end="b")
        box = re.search(
            rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", out.read_bytes()
        )
        width, height = float(box.group(1)), float(box.group(2))
        assert (round(width), round(height)) == (595, 842)  # A4 in points

    def test_entries_keep_the_order_they_were_given(self, tmp_path, config, out):
        rows = self.entries(tmp_path, 4)
        shuffled = [rows[2], rows[0], rows[3], rows[1]]
        result = write_pdf(shuffled, out, config, start="a", end="b")
        assert result.entry_ids == [3, 1, 4, 2]

    def test_each_entry_reports_the_page_it_starts_on(self, tmp_path, config, out):
        result = write_pdf(self.entries(tmp_path, 3), out, config, start="a", end="b")
        first = index_pages_for(3) + 1
        assert result.entry_pages == [first, first + 1, first + 2]

    def test_a_long_receipt_continues_over_further_pages(self, tmp_path, config, out):
        tall = scan_png(tmp_path / "scans" / "tall.png", size=(1348, 9000))
        short = scan_png(tmp_path / "scans" / "short.png", size=(1348, 900))
        rows = [row(1, scanned_path=str(tall)), row(2, scanned_path=str(short))]
        result = write_pdf(rows, out, config, start="a", end="b")
        first = index_pages_for(2) + 1
        assert result.entry_pages[1] - result.entry_pages[0] > 1
        assert result.entry_pages[0] == first
        assert result.pages == result.entry_pages[1]  # the short one is a single page

    def test_a_receipt_that_fits_when_shrunk_a_little_stays_on_one_page(
        self, tmp_path, config, out
    ):
        # Taller than the page, but only just: shrinking beats splitting.
        slightly_tall = scan_png(tmp_path / "scans" / "t.png", size=(1348, 2000))
        result = write_pdf(
            [row(1, scanned_path=str(slightly_tall))], out, config, start="a", end="b"
        )
        assert result.pages == index_pages_for(1) + 1

    def test_many_entries_spill_the_index_onto_more_pages(self, tmp_path, config, out):
        n = attachments.INDEX_ROWS_PER_PAGE * 2 + 5
        scan = scan_png(tmp_path / "scans" / "one.png", size=(600, 400))
        rows = [row(i, scanned_path=str(scan)) for i in range(1, n + 1)]
        result = write_pdf(rows, out, config, start="a", end="b")
        assert index_pages_for(n) == 3
        assert result.pages == 3 + n
        assert result.entry_pages[0] == 4

    def test_an_entry_with_no_image_still_gets_a_page(self, tmp_path, config, out):
        result = write_pdf(
            [row(1, source_kind="image")], out, config, start="a", end="b"
        )
        assert result.pages == index_pages_for(1) + 1
        assert result.by_origin == Counter({"none": 1})

    def test_origins_are_counted(self, tmp_path, config, out):
        box = mailbox(tmp_path, "<a@x>", "<b@x>")
        rows = [
            row(1, scanned_path=str(scan_png(tmp_path / "s.png"))),
            row(2, source_kind="email", source_path=str(box), email_message_id="<a@x>"),
            row(3, source_kind="email", source_path=str(box), email_message_id="<b@x>"),
            row(4, source_kind="image"),
        ]
        result = write_pdf(rows, out, config, start="a", end="b")
        assert result.by_origin == Counter(scan=1, redrawn=2, none=1)

    def test_no_entries_is_an_error_and_writes_nothing(self, config, out):
        with pytest.raises(AttachmentError):
            write_pdf([], out, config, start="a", end="b")
        assert not out.exists()

    def test_the_pdf_carries_no_author(self, tmp_path, config, out):
        write_pdf(self.entries(tmp_path, 1), out, config, start="a", end="b")
        pdf = out.read_bytes()
        assert b"/Author" not in pdf
        assert "Narmada".encode("utf-16-be") in pdf

    def test_pages_are_compressed_one_bit_images(self, tmp_path, config, out):
        write_pdf(self.entries(tmp_path, 5), out, config, start="a", end="b")
        pdf = out.read_bytes()
        assert b"CCITTFaxDecode" in pdf
        assert b"DCTDecode" not in pdf
        # 5 receipt pages + 1 index at A4/200dpi would be ~3 MB uncompressed.
        assert len(pdf) < 400_000

    def test_text_the_font_cannot_draw_does_not_break_the_export(
        self, tmp_path, config, out
    ):
        rows = self.entries(tmp_path, 1)
        rows[0]["name"] = "Kopi ☕ Tuku 🎉"
        rows[0]["category"] = "Makanan ☕"
        assert write_pdf(rows, out, config, start="a", end="b").entries == 1

    def test_it_reads_sqlite_rows_as_well_as_dicts(self, tmp_path, config, out):
        store = ReceiptStore(tmp_path / "receipts.db")
        try:
            for i in range(3):
                scan = scan_png(tmp_path / "scans" / f"db{i}.png")
                store.save(
                    make_record(
                        name=f"Shop {i}",
                        entry_date=f"2026-07-0{i + 1}",
                        scanned_path=str(scan),
                        source_kind="image",
                    )
                )
            rows = store.entries_between("2026-07-01", "2026-07-31")
        finally:
            store.close()
        result = write_pdf(rows, out, config, start="2026-07-01", end="2026-07-31")
        assert result.entries == 3
        assert result.by_origin == Counter(scan=3)


class TestProgressAndCancel:
    def entries(self, tmp_path, n):
        scan = scan_png(tmp_path / "scans" / "one.png", size=(600, 400))
        return [row(i, scanned_path=str(scan)) for i in range(1, n + 1)]

    def test_progress_counts_up_to_the_total(self, tmp_path, config, out):
        seen = []
        write_pdf(
            self.entries(tmp_path, 4),
            out,
            config,
            start="a",
            end="b",
            progress=lambda done, total: seen.append((done, total)),
        )
        assert seen[-1] == (4, 4)
        assert [done for done, _ in seen] == sorted(done for done, _ in seen)
        assert {total for _, total in seen} == {4}

    def test_cancelling_leaves_no_file_behind(self, tmp_path, config, out):
        stop = threading.Event()
        with pytest.raises(Cancelled):
            write_pdf(
                self.entries(tmp_path, 6),
                out,
                config,
                start="a",
                end="b",
                progress=lambda done, total: stop.set() if done == 2 else None,
                cancel=stop,
            )
        assert not out.exists()
        leftovers = list(out.parent.glob("*")) if out.parent.exists() else []
        assert leftovers == []

    def test_cancelling_stops_at_the_next_entry_not_at_the_end(
        self, tmp_path, config, out
    ):
        stop = threading.Event()
        seen = []

        def progress(done, total):
            seen.append(done)
            if done == 2:
                stop.set()

        with pytest.raises(Cancelled):
            write_pdf(
                self.entries(tmp_path, 6),
                out,
                config,
                start="a",
                end="b",
                progress=progress,
                cancel=stop,
            )
        assert seen == [1, 2]  # entries 3-6 were never drawn

    def test_cancelling_does_not_disturb_an_existing_report(
        self, tmp_path, config, out
    ):
        out.parent.mkdir(parents=True)
        out.write_bytes(b"previous report")
        stop = threading.Event()
        stop.set()
        with pytest.raises(Cancelled):
            write_pdf(
                self.entries(tmp_path, 3), out, config, start="a", end="b", cancel=stop
            )
        assert out.read_bytes() == b"previous report"

    def test_a_failed_write_leaves_no_partial_file(self, tmp_path, config):
        blocked = tmp_path / "out" / "report.pdf"
        blocked.mkdir(parents=True)  # a directory where the file should go
        with pytest.raises(OSError):
            write_pdf(self.entries(tmp_path, 2), blocked, config, start="a", end="b")
        assert not list(blocked.parent.glob("*.part"))


class TestReadOnly:
    def snapshot(self, root: Path) -> dict[str, tuple[int, str]]:
        return {
            str(p.relative_to(root)): (
                p.stat().st_size,
                hashlib.sha256(p.read_bytes()).hexdigest(),
            )
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    def test_redrawing_changes_nothing_but_the_pdf(self, tmp_path, config):
        box = mailbox(tmp_path, "<a@x>", "<b@x>")
        store = ReceiptStore(config.database_path)
        for i, message_id in enumerate(("<a@x>", "<b@x>")):
            store.save(
                make_record(
                    name=f"Shop {i}",
                    source_kind="email",
                    source_path=str(box),
                    email_message_id=message_id,
                    scanned_path=None,
                )
            )
        rows = store.entries_between("2026-07-01", "2026-07-31")
        store.close()

        before = self.snapshot(tmp_path)
        destination = tmp_path / "elsewhere" / "report.pdf"
        result = write_pdf(rows, destination, config, start="a", end="b")
        assert result.by_origin == Counter(redrawn=2)

        after = self.snapshot(tmp_path)
        added = set(after) - set(before)
        assert added == {"elsewhere/report.pdf"}
        assert {k: v for k, v in after.items() if k in before} == before


# -- the index page ------------------------------------------------------------
class TestIndexModel:
    def test_it_lists_every_entry_with_its_page(self):
        rows = [row(1, name="A"), row(2, name="B", amount=2500.5)]
        model = attachments.index_model(rows, [2, 3], "2026-07-01", "2026-07-31", "IDR")
        assert [e.number for e in model.entries] == [1, 2]
        assert [e.page for e in model.entries] == [2, 3]
        assert [e.detail for e in model.entries] == ["A", "B"]
        assert model.entries[1].amount == "IDR 2,500.50"

    def test_totals_are_per_currency_and_match_the_report(self):
        rows = [
            row(1, amount=1000.0, currency="IDR"),
            row(2, amount=5.5, currency="USD"),
            row(3, amount=2000.0, currency="IDR"),
        ]
        model = attachments.index_model(rows, [2, 3, 4], "a", "b", "IDR")
        summary = report.summarize(rows, "a", "b", "IDR")
        assert model.totals == report.format_totals(summary.totals)
        assert model.totals == "IDR 3,000 · USD 5.50"

    def test_a_legacy_row_without_a_currency_uses_the_default(self):
        model = attachments.index_model([row(1, currency=None)], [2], "a", "b", "IDR")
        assert model.entries[0].amount.startswith("IDR ")

    def test_an_unreadable_amount_is_blank_not_a_crash(self):
        model = attachments.index_model([row(1, amount=None)], [2], "a", "b", "IDR")
        assert model.entries[0].amount == ""

    def test_undrawable_characters_are_stripped(self):
        model = attachments.index_model(
            [row(1, name="Kopi ☕ Tuku 🎉")], [2], "a", "b", "IDR"
        )
        assert "☕" not in model.entries[0].detail
        assert "🎉" not in model.entries[0].detail
        assert model.entries[0].detail == "Kopi Tuku"

    def test_the_title_names_the_range(self):
        model = attachments.index_model(
            [row(1)], [2], "2026-07-01", "2026-07-31", "IDR"
        )
        assert "2026-07-01" in model.title and "2026-07-31" in model.title


class TestDefaultFilename:
    def test_it_names_the_range_and_is_not_the_csv_name(self):
        name = attachments.default_filename("2026-07-01", "2026-07-31")
        assert name == "receipts_2026-07-01_to_2026-07-31_attachments.pdf"
        assert name != report.default_filename("2026-07-01", "2026-07-31")


# -- the shared money formatting moved into report.py --------------------------
class TestMoneyFormatting:
    def test_whole_amounts_have_no_decimals(self):
        assert report.format_money(44000.0, "IDR") == "IDR 44,000"

    def test_fractions_keep_two_places(self):
        assert report.format_money(56.48, "USD") == "USD 56.48"

    def test_no_currency_is_just_the_number(self):
        assert report.format_money(1234.5) == "1,234.50"

    def test_totals_are_sorted_by_currency(self):
        assert (
            report.format_totals({"USD": 76.48, "IDR": 69000.0})
            == "IDR 69,000 · USD 76.48"
        )

    def test_the_app_still_exposes_the_same_helpers(self):
        from receiptscanner import app

        assert app._format_money(44000.0, "IDR") == "IDR 44,000"
        assert app._format_totals({"IDR": 1.0}) == "IDR 1"


class TestCsvIsUntouched:
    def test_the_csv_columns_do_not_gain_attachment_fields(self):
        assert report.COLUMNS == (
            "Date",
            "Category",
            "Expense Detail",
            "Amount",
            "Currency",
        )
