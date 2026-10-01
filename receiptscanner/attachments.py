"""A PDF of the receipts behind a report: an index page, then one page per entry.

Finance teams want the receipt itself next to the figures, and the CSV (report.py)
carries only the figures. Kept apart from the UI, like report.py, so the export
can be exercised without a window.

Where each entry's picture comes from, in order:

1. its stored scan (photos, and e-receipts rendered on import);
2. a mailbox, for e-receipts saved before the render path existed and so never
   given an image: the message is found by Message-ID and drawn with the same
   renderer a fresh import uses;
3. the original photo, when the scan has gone but the photo is still there;
4. nothing: a marked page carrying the stored text, so no entry silently drops out.

Everything here is read-only. Redrawing parses the mailbox into a temporary
directory, runs no OCR, saves no PNG and writes no database row, so exporting
twice leaves the user's folders exactly as they were.
"""

from __future__ import annotations

import math
import os
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import cv2
from PIL import Image, ImageDraw, ImageFont

from . import APP_TITLE, imaging, mail, render, report
from .config import Config

# A4 portrait at 200 dpi. 200 keeps a 1348 px scan close to its native size.
PAGE_DPI = 200
PAGE_W, PAGE_H = 1654, 2339
MARGIN = 80

# Scans are already black on white, and renders are black text on white, so a
# fixed threshold loses nothing, and 1-bit pages let Pillow write CCITT G4: about
# a tenth of the size of 8-bit JPEG pages, and crisp where JPEG blurs text.
THRESHOLD = 170

# When fitting a whole receipt on one page would shrink it below this share of the
# size it would have at full page width, it is sliced across pages instead, so
# the text stays readable.
MIN_FIT_FRACTION = 0.6
SLICE_OVERLAP = 40

CAPTION_H = 190
CONTINUATION_CAPTION_H = 90
INDEX_ROWS_PER_PAGE = 38
INDEX_ROW_H = 48
PLACEHOLDER_MAX_LINES = 40

ORIGIN_LABELS = {
    "scan": "Scan",
    "redrawn": "Redrawn from mailbox",
    "original": "Original photo",
    "none": "No image on file",
}

_MAILBOX_SUFFIXES = (".mbox", ".mbx")


class AttachmentError(RuntimeError):
    """The export cannot proceed (nothing to attach)."""


class Cancelled(Exception):
    """The caller asked to stop. Nothing has been written."""


@dataclass(frozen=True)
class PageSource:
    """One entry's picture, and where it came from."""

    image: Image.Image | None
    origin: str  # "scan" | "redrawn" | "original" | "none"


@dataclass(frozen=True)
class IndexEntry:
    number: int
    date: str
    category: str
    detail: str
    amount: str
    page: int


@dataclass(frozen=True)
class IndexModel:
    title: str
    entries: list[IndexEntry]
    totals: str


@dataclass
class AttachmentResult:
    path: Path
    entries: int
    pages: int
    by_origin: Counter = field(default_factory=Counter)
    entry_ids: list[Any] = field(default_factory=list)
    entry_pages: list[int] = field(default_factory=list)


# -- row access ---------------------------------------------------------------
def _field(row: Mapping, key: str) -> Any:
    """Works for dicts and sqlite3.Row alike (the latter raises IndexError)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _is_mailbox(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _MAILBOX_SUFFIXES


def default_filename(start: str, end: str) -> str:
    return f"receipts_{start}_to_{end}_attachments.pdf"


def index_pages_needed(entries: int) -> int:
    return max(1, math.ceil(entries / INDEX_ROWS_PER_PAGE))


# -- finding the picture ------------------------------------------------------
class MailboxCache:
    """Each mailbox is parsed once per export, however many entries came from it.

    Attachments `iter_mbox` extracts go to a temporary directory that disappears
    with the cache, so nothing is ever written beside the user's mailbox.
    """

    def __init__(self) -> None:
        self._scratch = tempfile.TemporaryDirectory(prefix="narmada-mailbox-")
        self._indexes: dict[Path, dict[str, mail.EmailReceipt]] = {}

    def __enter__(self) -> "MailboxCache":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self._scratch.cleanup()

    def find(
        self, mailbox_path: str | Path, message_id: str
    ) -> mail.EmailReceipt | None:
        key = Path(mailbox_path)
        if key not in self._indexes:
            self._indexes[key] = self._load(key)
        return self._indexes[key].get((message_id or "").strip())

    def _load(self, path: Path) -> dict[str, mail.EmailReceipt]:
        folder = Path(self._scratch.name) / str(len(self._indexes))
        index: dict[str, mail.EmailReceipt] = {}
        try:
            for receipt in mail.iter_mbox(path, folder):
                if receipt.message_id and receipt.message_id not in index:
                    index[receipt.message_id] = receipt
        except (mail.MailboxError, OSError):
            pass  # an unreadable mailbox just means its entries get a placeholder
        return index


def expected_origin(row: Mapping) -> str:
    """What `resolve_image` will probably do, without loading or parsing anything.

    The dialog uses this for its preview. It is an estimate: a scan that exists
    but cannot be decoded, or a Message-ID the mailbox does not hold, resolve
    differently.
    """
    scan = _field(row, "scanned_path")
    if scan and Path(scan).is_file():
        return "scan"
    source = _field(row, "source_path")
    if source and Path(source).is_file():
        if _is_mailbox(source):
            return "redrawn" if _field(row, "email_message_id") else "none"
        return "original"
    return "none"


def _open_scan(path: str | Path) -> Image.Image | None:
    try:
        with Image.open(path) as image:
            return image.convert("L")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _open_original(path: str | Path) -> Image.Image | None:
    try:
        bgr, _info = imaging.load_image(Path(path))
    except imaging.ImageLoadError:
        return None
    return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))


def _redraw(
    row: Mapping, mailbox_path: str, config: Config, mailboxes: MailboxCache
) -> Image.Image | None:
    receipt = mailboxes.find(mailbox_path, _field(row, "email_message_id") or "")
    if receipt is None:
        return None
    try:
        return Image.fromarray(render.render_email(receipt, config.email_render))
    except render.RenderError:
        return None


def resolve_image(row: Mapping, config: Config, mailboxes: MailboxCache) -> PageSource:
    scan = _field(row, "scanned_path")
    if scan and Path(scan).is_file():
        image = _open_scan(scan)
        if image is not None:
            return PageSource(image, "scan")

    source = _field(row, "source_path")
    if source and Path(source).is_file():
        if _is_mailbox(source):
            image = _redraw(row, str(source), config, mailboxes)
            if image is not None:
                return PageSource(image, "redrawn")
        else:
            image = _open_original(source)
            if image is not None:
                return PageSource(image, "original")

    return PageSource(None, "none")


# -- text ---------------------------------------------------------------------
def _entry_fields(row: Mapping, default_currency: str) -> tuple[str, str, str, str]:
    """Date, category, detail and formatted amount, cleaned for drawing."""
    try:
        amount = report.format_money(
            float(_field(row, "amount")), report._currency_of(row, default_currency)
        )
    except (TypeError, ValueError):
        amount = ""
    return (
        render._sanitize(str(_field(row, "entry_date") or "")),
        render._sanitize(str(_field(row, "category") or "")),
        render._sanitize(str(_field(row, "name") or "")),
        amount,
    )


def index_model(
    rows: Sequence[Mapping],
    entry_pages: Sequence[int],
    start: str,
    end: str,
    default_currency: str,
) -> IndexModel:
    entries = []
    for number, (row, page) in enumerate(zip(rows, entry_pages), start=1):
        date, category, detail, amount = _entry_fields(row, default_currency)
        entries.append(IndexEntry(number, date, category, detail, amount, page))
    summary = report.summarize(rows, start, end, default_currency)
    return IndexModel(
        title=f"Receipts {start} – {end}",
        entries=entries,
        totals=report.format_totals(summary.totals),
    )


class _Fonts:
    def __init__(self, hint: str = "") -> None:
        self._hint = hint
        self._cache: dict[int, ImageFont.ImageFont] = {}

    def __call__(self, size: int) -> ImageFont.ImageFont:
        if size not in self._cache:
            self._cache[size] = render._load_font(self._hint, size)
        return self._cache[size]


def _fit(draw: ImageDraw.ImageDraw, text: str, font, limit: int) -> str:
    """Shorten with an ellipsis until `text` fits in `limit` pixels."""
    if draw.textlength(text, font=font) <= limit:
        return text
    while text and draw.textlength(text + "…", font=font) > limit:
        text = text[:-1]
    return text.rstrip() + "…"


def _blank() -> Image.Image:
    return Image.new("L", (PAGE_W, PAGE_H), 255)


def _binarize(page: Image.Image) -> Image.Image:
    return page.point(lambda value: 255 if value >= THRESHOLD else 0).convert("1")


# -- index pages --------------------------------------------------------------
def _index_pages(model: IndexModel, fonts: _Fonts) -> list[Image.Image]:
    right = PAGE_W - MARGIN
    columns = {"number": MARGIN, "date": MARGIN + 90, "category": MARGIN + 330}
    detail_x = MARGIN + 720
    amount_right = right - 130

    pages = []
    chunks = [
        model.entries[i : i + INDEX_ROWS_PER_PAGE]
        for i in range(0, max(len(model.entries), 1), INDEX_ROWS_PER_PAGE)
    ] or [[]]
    for position, chunk in enumerate(chunks):
        page = _blank()
        draw = ImageDraw.Draw(page)
        draw.text((MARGIN, MARGIN), model.title, font=fonts(54), fill=0)
        draw.text(
            (MARGIN, MARGIN + 74),
            f"{len(model.entries)} entr{'y' if len(model.entries) == 1 else 'ies'}"
            f" · page {position + 1} of {len(chunks)} of the index",
            font=fonts(28),
            fill=0,
        )

        y = MARGIN + 150
        head = fonts(26)
        draw.text((columns["number"], y), "#", font=head, fill=0)
        draw.text((columns["date"], y), "Date", font=head, fill=0)
        draw.text((columns["category"], y), "Category", font=head, fill=0)
        draw.text((detail_x, y), "Expense Detail", font=head, fill=0)
        draw.text(
            (amount_right - draw.textlength("Amount", font=head), y),
            "Amount",
            font=head,
            fill=0,
        )
        draw.text(
            (right - draw.textlength("Page", font=head), y), "Page", font=head, fill=0
        )
        y += 42
        draw.line([(MARGIN, y), (right, y)], fill=0, width=2)
        y += 12

        body = fonts(30)
        for entry in chunk:
            draw.text((columns["number"], y), str(entry.number), font=body, fill=0)
            draw.text((columns["date"], y), entry.date, font=body, fill=0)
            draw.text(
                (columns["category"], y),
                _fit(draw, entry.category, body, detail_x - columns["category"] - 24),
                font=body,
                fill=0,
            )
            draw.text(
                (detail_x, y),
                _fit(draw, entry.detail, body, amount_right - 240 - detail_x),
                font=body,
                fill=0,
            )
            amount = _fit(draw, entry.amount, body, 260)
            draw.text(
                (amount_right - draw.textlength(amount, font=body), y),
                amount,
                font=body,
                fill=0,
            )
            number = str(entry.page)
            draw.text(
                (right - draw.textlength(number, font=body), y),
                number,
                font=body,
                fill=0,
            )
            y += INDEX_ROW_H

        if position == len(chunks) - 1 and model.totals:
            y += 16
            draw.line([(MARGIN, y), (right, y)], fill=0, width=2)
            y += 18
            draw.text((MARGIN, y), "Total", font=fonts(36), fill=0)
            totals = _fit(draw, model.totals, fonts(36), PAGE_W - 2 * MARGIN - 160)
            draw.text(
                (right - draw.textlength(totals, font=fonts(36)), y),
                totals,
                font=fonts(36),
                fill=0,
            )
        pages.append(_binarize(page))
    return pages


# -- receipt pages ------------------------------------------------------------
def _placeholder(row: Mapping, fonts: _Fonts) -> Image.Image:
    """Stand-in for an entry with no image: a notice and whatever text was kept."""
    width = PAGE_W - 2 * MARGIN
    heading, body = fonts(40), fonts(30)
    scratch = ImageDraw.Draw(Image.new("L", (width, 10), 255))

    lines: list[str] = []
    for paragraph in str(_field(row, "ocr_text") or "").splitlines():
        lines.extend(
            render._wrap(scratch, render._sanitize(paragraph), body, width) or [""]
        )
    if len(lines) > PLACEHOLDER_MAX_LINES:
        lines = lines[:PLACEHOLDER_MAX_LINES] + ["…"]

    height = 120 + 44 * len(lines)
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    draw.text((0, 0), "No image on file for this entry.", font=heading, fill=0)
    if lines:
        draw.text((0, 62), "Text recorded when it was saved:", font=body, fill=0)
    y = 120
    for line in lines:
        draw.text((0, y), line, font=body, fill=0)
        y += 44
    return image


def _resized(image: Image.Image, scale: float) -> Image.Image:
    if scale == 1.0:
        return image
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _slices(image: Image.Image, origin: str) -> list[Image.Image]:
    """The image cut to fit the pages it will sit on, never enlarged.

    Whole when fitting it to one page keeps it readable; otherwise cut at full
    page width, with a little overlap so a line on a cut is not lost.
    """
    avail_w = PAGE_W - 2 * MARGIN
    first_h = PAGE_H - 2 * MARGIN - CAPTION_H
    continued_h = PAGE_H - 2 * MARGIN - CONTINUATION_CAPTION_H

    width_scale = min(1.0, avail_w / image.width)
    fit_scale = min(width_scale, first_h / image.height)

    if fit_scale >= MIN_FIT_FRACTION * width_scale:
        pieces = [_resized(image, fit_scale)]
    else:
        scaled = _resized(image, width_scale)
        pieces, y = [], 0
        while True:
            limit = first_h if not pieces else continued_h
            pieces.append(
                scaled.crop((0, y, scaled.width, min(scaled.height, y + limit)))
            )
            if y + limit >= scaled.height:
                break
            y += limit - SLICE_OVERLAP

    if origin == "original":
        # A camera photo is not black on white: dither it rather than threshold it.
        pieces = [piece.convert("1").convert("L") for piece in pieces]
    return pieces


def _receipt_pages(
    row: Mapping,
    position: int,
    total: int,
    source: PageSource,
    default_currency: str,
    fonts: _Fonts,
) -> list[Image.Image]:
    date, category, detail, amount = _entry_fields(row, default_currency)
    heading = " · ".join(
        part for part in (f"{position} / {total}", date, category) if part
    )
    subject = " · ".join(part for part in (detail, amount) if part)
    entry_id = _field(row, "id")
    meta = ORIGIN_LABELS[source.origin] + (
        f" · ID {entry_id}" if entry_id is not None else ""
    )

    image = source.image if source.image is not None else _placeholder(row, fonts)
    pieces = _slices(image, source.origin)

    limit = PAGE_W - 2 * MARGIN
    pages = []
    for number, piece in enumerate(pieces, start=1):
        page = _blank()
        draw = ImageDraw.Draw(page)
        if number == 1:
            draw.text(
                (MARGIN, MARGIN),
                _fit(draw, heading, fonts(40), limit),
                font=fonts(40),
                fill=0,
            )
            draw.text(
                (MARGIN, MARGIN + 58),
                _fit(draw, subject, fonts(34), limit),
                font=fonts(34),
                fill=0,
            )
            draw.text(
                (MARGIN, MARGIN + 110),
                _fit(draw, meta, fonts(26), limit),
                font=fonts(26),
                fill=0,
            )
            top = MARGIN + CAPTION_H
        else:
            line = f"{position} / {total} · {detail} (continued {number}/{len(pieces)})"
            draw.text(
                (MARGIN, MARGIN),
                _fit(draw, line, fonts(30), limit),
                font=fonts(30),
                fill=0,
            )
            top = MARGIN + CONTINUATION_CAPTION_H
        page.paste(piece, ((PAGE_W - piece.width) // 2, top))
        pages.append(_binarize(page))
    return pages


# -- the export ---------------------------------------------------------------
def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def write_pdf(
    rows: Iterable[Mapping],
    path: Path,
    config: Config,
    *,
    start: str,
    end: str,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> AttachmentResult:
    """Write the index and every receipt to `path`; return what was written.

    `rows` keep the order they are given, so the PDF lines up with a CSV made from
    the same query. Safe to run on a worker thread: it touches no widgets. Pages
    are all built before anything is written, and the file lands by rename, so a
    cancel or a failure never leaves a partial PDF or damages an earlier one.
    """
    rows = list(rows)
    if not rows:
        raise AttachmentError("There are no entries to attach.")

    path = Path(path)
    total = len(rows)
    currency = config.currency
    fonts = _Fonts(str(config.email_render.get("font_path") or ""))
    first_receipt_page = index_pages_needed(total) + 1

    receipt_pages: list[Image.Image] = []
    entry_pages: list[int] = []
    origins: Counter = Counter()

    with MailboxCache() as mailboxes:
        for position, row in enumerate(rows, start=1):
            _check(cancel)
            source = resolve_image(row, config, mailboxes)
            origins[source.origin] += 1
            entry_pages.append(first_receipt_page + len(receipt_pages))
            receipt_pages.extend(
                _receipt_pages(row, position, total, source, currency, fonts)
            )
            if progress is not None:
                progress(position, total)
    _check(cancel)

    model = index_model(rows, entry_pages, start, end, currency)
    pages = _index_pages(model, fonts) + receipt_pages

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    try:
        pages[0].save(
            partial,
            "PDF",
            save_all=True,
            append_images=pages[1:],
            resolution=float(PAGE_DPI),
            title=model.title,
            producer=APP_TITLE,
        )
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    return AttachmentResult(
        path=path,
        entries=total,
        pages=len(pages),
        by_origin=origins,
        entry_ids=[_field(row, "id") for row in rows],
        entry_pages=entry_pages,
    )
