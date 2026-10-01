"""Mailbox ingest: HTML flattening, part extraction, and merchant guessing."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers import build_mbox, make_message
from receiptscanner import mail
from receiptscanner.mail import (
    EmailReceipt,
    MailboxError,
    _trim_address,
    count_messages,
    html_to_text,
    iter_mbox,
    structured_rows,
)

TABLE = """
<html><head><title>x</title><style>td{color:red}</style></head><body>
<table>
  <tr><td>Fare</td><td>33.500</td></tr>
  <tr><td>Platform Fee</td><td>10.500</td></tr>
  <tr><td>Total Paid</td><td>44.000</td></tr>
</table></body></html>
"""


class TestStructuredRows:
    def test_each_table_row_keeps_its_cells_apart(self):
        assert structured_rows(TABLE) == [
            ["Fare", "33.500"],
            ["Platform Fee", "10.500"],
            ["Total Paid", "44.000"],
        ]

    def test_only_the_innermost_rows_of_nested_layout_tables(self):
        html = """
        <table><tr><td>
          <table>
            <tr><td>Fare</td><td>1.000</td></tr>
            <tr><td>Fee</td><td>2.000</td></tr>
            <tr><td>Total</td><td>3.000</td></tr>
          </table>
        </td></tr></table>"""
        assert structured_rows(html) == [
            ["Fare", "1.000"],
            ["Fee", "2.000"],
            ["Total", "3.000"],
        ]

    def test_empty_cells_and_whitespace_are_dropped(self):
        html = (
            "<table>"
            "<tr><td>  Fare \n </td><td></td><td>  1.000 </td></tr>"
            "<tr><td>Fee</td><td>&nbsp;</td><td>2.000</td></tr>"
            "<tr><td>Total</td><td>3.000</td></tr>"
            "</table>"
        )
        rows = structured_rows(html)
        assert rows[0] == ["Fare", "1.000"]
        assert rows[2] == ["Total", "3.000"]

    def test_scripts_styles_and_titles_never_leak_into_the_text(self):
        flat = " ".join(" ".join(row) for row in structured_rows(TABLE))
        assert "color:red" not in flat
        assert "x" not in flat.split()

    def test_header_rows_are_read_like_data_rows(self):
        html = (
            "<table><tr><th>Item</th><th>Price</th></tr>"
            "<tr><td>A</td><td>1</td></tr><tr><td>B</td><td>2</td></tr></table>"
        )
        assert structured_rows(html)[0] == ["Item", "Price"]

    def test_non_table_layouts_fall_back_to_one_cell_per_line(self):
        html = "<div>Total</div><div>44.000</div>"
        assert structured_rows(html) == [["Total"], ["44.000"]]

    def test_two_table_rows_are_too_few_so_it_falls_back(self):
        html = (
            "<table><tr><td>A</td><td>1</td></tr><tr><td>B</td><td>2</td></tr></table>"
        )
        assert all(len(row) == 1 for row in structured_rows(html))

    def test_blank_documents_have_no_rows(self):
        assert structured_rows("") == []
        assert structured_rows("<p>   </p>") == []


class TestHtmlToText:
    def test_rows_are_joined_with_three_spaces_one_per_line(self):
        assert html_to_text(TABLE) == (
            "Fare   33.500\nPlatform Fee   10.500\nTotal Paid   44.000"
        )

    def test_is_exactly_the_joined_structured_rows(self):
        expected = "\n".join("   ".join(row) for row in structured_rows(TABLE))
        assert html_to_text(TABLE) == expected

    def test_empty(self):
        assert html_to_text("") == ""


class TestIterMbox:
    def read(self, tmp_path, *messages, **kwargs):
        box = build_mbox(tmp_path / "in.mbox", list(messages))
        return list(iter_mbox(box, tmp_path / "attachments", **kwargs))

    def test_plain_text_message(self, tmp_path):
        (item,) = self.read(tmp_path, make_message(plain="Total 44.000"))
        assert item.body_text == "Total 44.000"
        assert item.body_html == ""

    def test_html_only_message_is_flattened_and_the_markup_is_kept(self, tmp_path):
        (item,) = self.read(tmp_path, make_message(html=TABLE))
        assert "Total Paid   44.000" in item.body_text
        assert "<table>" in item.body_html

    def test_multipart_alternative_keeps_the_html_beside_the_plain_text(self, tmp_path):
        # The renderer needs the markup. The plain alternative used to win and
        # the HTML was thrown away, leaving nothing to draw.
        (item,) = self.read(tmp_path, make_message(plain="Total 44.000", html=TABLE))
        assert item.body_text == "Total 44.000"
        assert "Total Paid" in item.body_html

    def test_headers(self, tmp_path):
        (item,) = self.read(
            tmp_path,
            make_message(
                subject="Pesanan — Nasi Goreng",
                sender="Grab Indonesia <no-reply@grab.com>",
                message_id="  <abc@grab.com>  ",
                plain="x",
            ),
        )
        assert item.subject == "Pesanan — Nasi Goreng"
        assert (item.sender_name, item.sender_email) == (
            "Grab Indonesia",
            "no-reply@grab.com",
        )
        assert item.message_id == "<abc@grab.com>"
        assert item.index == 0

    def test_a_bare_address_doubles_as_the_sender_name(self, tmp_path):
        (item,) = self.read(tmp_path, make_message(sender="a@b.com", plain="x"))
        assert item.sender_name == "a@b.com"

    def test_the_date_header_supplies_the_fallback_date(self, tmp_path):
        (item,) = self.read(tmp_path, make_message(plain="x"))
        assert item.date_iso == "2026-07-14"

    def test_the_header_s_own_timezone_decides_the_calendar_day(self, tmp_path):
        # Characterisation, and a documented limit: 23:30 at +07:00 is still the
        # 14th even though it is already the 15th in UTC.
        late = datetime(2026, 7, 14, 23, 30, tzinfo=timezone(timedelta(hours=7)))
        (item,) = self.read(tmp_path, make_message(plain="x", when=late))
        assert item.date_iso == "2026-07-14"

    def test_missing_date_and_message_id(self, tmp_path):
        (item,) = self.read(
            tmp_path, make_message(plain="x", when=None, message_id=None)
        )
        assert item.date is None
        assert item.date_iso == ""
        assert item.message_id == ""

    def test_messages_are_indexed_in_order(self, tmp_path):
        items = self.read(
            tmp_path,
            make_message(subject="a", plain="1"),
            make_message(subject="b", plain="2"),
            make_message(subject="c", plain="3"),
        )
        assert [(i.index, i.subject) for i in items] == [(0, "a"), (1, "b"), (2, "c")]

    def test_empty_mailbox(self, tmp_path):
        assert self.read(tmp_path) == []

    def test_one_unreadable_message_does_not_stop_the_import(
        self, tmp_path, monkeypatch
    ):
        real = mail._extract_parts

        def flaky(message, index, *args):
            if index == 1:
                raise ValueError("boom")
            return real(message, index, *args)

        monkeypatch.setattr(mail, "_extract_parts", flaky)
        items = self.read(
            tmp_path,
            make_message(plain="fine one"),
            make_message(plain="breaks"),
            make_message(plain="fine two"),
        )
        assert [i.body_text for i in items][0] == "fine one"
        assert items[1].body_text.startswith("[could not read message: boom")
        assert items[2].body_text == "fine two"

    def test_missing_file_is_a_mailbox_error(self, tmp_path):
        with pytest.raises(MailboxError):
            list(iter_mbox(tmp_path / "nope.mbox", tmp_path))

    def test_count_messages(self, tmp_path):
        box = build_mbox(
            tmp_path / "in.mbox", [make_message(plain="a"), make_message(plain="b")]
        )
        assert count_messages(box) == 2


class TestAttachments:
    BIG = b"\xff\xd8" + b"x" * 9000  # over the 8 KB floor

    def test_a_real_attachment_is_written_to_disk(self, tmp_path):
        box = build_mbox(
            tmp_path / "in.mbox",
            [make_message(plain="x", images=[("photo.jpg", self.BIG)])],
        )
        (item,) = iter_mbox(box, tmp_path / "att")
        assert len(item.images) == 1
        assert item.images[0].name == "msg0000_01_photo.jpg"
        assert item.images[0].read_bytes() == self.BIG

    def test_logos_and_spacers_under_the_floor_are_ignored(self, tmp_path):
        box = build_mbox(
            tmp_path / "in.mbox",
            [make_message(plain="x", images=[("logo.png", b"x" * 500)])],
        )
        (item,) = iter_mbox(box, tmp_path / "att")
        assert item.images == []
        assert not (tmp_path / "att").exists()

    def test_the_floor_is_exactly_eight_kilobytes(self, tmp_path):
        box = build_mbox(
            tmp_path / "in.mbox",
            [
                make_message(
                    plain="x",
                    images=[("under.jpg", b"x" * 8191), ("exact.jpg", b"x" * 8192)],
                )
            ],
        )
        (item,) = iter_mbox(box, tmp_path / "att")
        assert [p.name for p in item.images] == ["msg0000_01_exact.jpg"]

    def test_the_floor_can_be_overridden(self, tmp_path):
        box = build_mbox(
            tmp_path / "in.mbox",
            [make_message(plain="x", images=[("small.jpg", b"x" * 500)])],
        )
        (item,) = iter_mbox(box, tmp_path / "att", min_attachment_bytes=100)
        assert len(item.images) == 1

    def test_unsafe_filenames_are_sanitised(self, tmp_path):
        box = build_mbox(
            tmp_path / "in.mbox",
            [make_message(plain="x", images=[("re ceipt (1)!.jpg", self.BIG)])],
        )
        (item,) = iter_mbox(box, tmp_path / "att")
        name = item.images[0].name
        assert "/" not in name and " " not in name and "!" not in name
        assert item.images[0].parent == Path(tmp_path / "att")

    def test_several_attachments_are_numbered(self, tmp_path):
        box = build_mbox(
            tmp_path / "in.mbox",
            [
                make_message(
                    plain="x", images=[("a.jpg", self.BIG), ("b.jpg", self.BIG)]
                )
            ],
        )
        (item,) = iter_mbox(box, tmp_path / "att")
        assert [p.name for p in item.images] == ["msg0000_01_a.jpg", "msg0000_02_b.jpg"]

    def test_remote_images_in_html_are_never_fetched(self, tmp_path, monkeypatch):
        import socket

        def forbidden(*args, **kwargs):
            raise AssertionError("the mail reader must not open a network socket")

        monkeypatch.setattr(socket, "socket", forbidden)
        html = '<p>Hi</p><img src="https://tracker.example.com/pixel.gif">'
        box = build_mbox(tmp_path / "in.mbox", [make_message(html=html)])
        (item,) = iter_mbox(box, tmp_path / "att")
        assert item.images == []


class TestEmailReceipt:
    def receipt(self, **kw):
        return EmailReceipt(index=0, mbox_path=Path("x.mbox"), **kw)

    def test_label_prefers_subject_then_sender_then_position(self):
        assert self.receipt(subject="S", sender_name="N").label == "S"
        assert self.receipt(sender_name="N").label == "N"
        assert self.receipt().label == "message 1"

    def test_as_row(self):
        row = self.receipt(
            subject="S",
            sender_name="N",
            sender_email="n@x.com",
            message_id="<m>",
            date=datetime(2026, 7, 14, tzinfo=timezone.utc),
        ).as_row()
        assert row["email_message_id"] == "<m>"
        assert row["email_subject"] == "S"
        assert row["email_from"] == "N <n@x.com>"
        assert row["email_date"].startswith("2026-07-14")

    def test_as_row_nulls_what_is_missing(self):
        row = self.receipt().as_row()
        assert row["email_message_id"] is None
        assert row["email_from"] is None
        assert row["email_date"] is None


class TestMerchantGuess:
    def guess(self, body, **kw):
        return EmailReceipt(
            index=0, mbox_path=Path("x"), body_text=body, **kw
        ).merchant_guess()

    @pytest.mark.parametrize(
        "line, expected",
        [
            ("Pesanan dari: Warung Baru", "Warung Baru"),
            ("Order from Pizza Place", "Pizza Place"),
            ("Merchant: Kopi Kenangan", "Kopi Kenangan"),
            ("Sold by: Toko Elektronik", "Toko Elektronik"),
        ],
    )
    def test_explicit_merchant_lines(self, line, expected):
        assert self.guess(f"Hello\n{line}\nTotal 1.000") == expected

    def test_the_outlet_address_is_cut_off(self):
        line = "Pesanan dari: Nonna Pasta, Jl. Mangga I No.9, RT.8/RW.8"
        assert self.guess(line) == "Nonna Pasta"

    def test_falls_back_to_the_sender(self):
        assert self.guess("Total 1.000", sender_name="Grab") == "Grab"
        assert self.guess("Total 1.000", sender_email="a@b.com") == "a@b.com"

    def test_nothing_known(self):
        assert self.guess("") == ""


class TestTrimAddress:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("Nonna Pasta Jl. Mangga No.9", "Nonna Pasta"),
            ("Kopi Kenangan, Jakarta", "Kopi Kenangan"),
            ("Toko Maju Ruko Blok A", "Toko Maju"),
            ("  Plain Name  ", "Plain Name"),
        ],
    )
    def test_cuts_at_the_first_address_marker_or_comma(self, value, expected):
        assert _trim_address(value) == expected

    def test_length_is_capped(self):
        assert len(_trim_address("A" * 200)) == 70
