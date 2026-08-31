"""Reading receipts out of a Gmail .mbox export.

An exported mailbox holds two quite different kinds of receipt:

* **Photos**, attached as image parts. Those are written out to disk and go
  through the ordinary OpenCV + Tesseract pipeline, unchanged.
* **HTML e-receipts** (Grab, GoTo, airline and hotel confirmations...), where
  the figures live in the markup and there is no image at all. This module
  reduces those to rows of cells; `render.py` draws them, and they then go
  through OCR like any other receipt, so a human has a picture to check the
  suggested figures against.

Remote images referenced by `<img src="https://...">` are deliberately never
fetched: this app makes no network calls, and in a marketing email those URLs
are mostly logos and tracking pixels anyway.

mbox itself needs no third-party library — the standard `mailbox` and `email`
modules handle the format. BeautifulSoup is used for the HTML-to-text step,
where hand-rolled tag stripping does badly.
"""

from __future__ import annotations

import mailbox
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Iterator

from bs4 import BeautifulSoup

# Below this an image part is a logo, signature image or spacer, not a receipt.
MIN_ATTACHMENT_BYTES = 8 * 1024

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Lines that name the merchant on common Indonesian e-receipts.
MERCHANT_PREFIXES = (
    "pesanan dari",
    "order from",
    "dipesan dari",
    "merchant",
    "restoran",
    "toko",
    "outlet",
    "store",
    "sold by",
    "penjual",
)


# Food-delivery receipts print the outlet's full street address right after its
# name on one line; cut at the first address marker or comma so the name field
# does not fill up with "Jl. Mangga I No.9, RT.8/RW.8, ...".
_ADDRESS_MARKER = re.compile(
    r"\b(jl\.?|jalan|blok|ruko|gedung|lantai|kav\.?|rt\.?\s*\d|rw\.?\s*\d|no\.?\s*\d)\b",
    re.IGNORECASE,
)


def _trim_address(value: str, limit: int = 70) -> str:
    match = _ADDRESS_MARKER.search(value)
    if match:
        value = value[: match.start()]
    value = value.split(",")[0]
    return " ".join(value.split()).strip(" -–—,:")[:limit]


class MailboxError(RuntimeError):
    pass


@dataclass
class EmailReceipt:
    """One message from the mailbox, reduced to what the pipeline needs."""

    index: int
    mbox_path: Path
    subject: str = ""
    sender_name: str = ""
    sender_email: str = ""
    message_id: str = ""
    date: datetime | None = None
    body_text: str = ""
    body_html: str = ""
    images: list[Path] = field(default_factory=list)

    @property
    def label(self) -> str:
        """What to show in the queue."""
        return self.subject or self.sender_name or f"message {self.index + 1}"

    @property
    def date_iso(self) -> str:
        return self.date.date().isoformat() if self.date else ""

    def merchant_guess(self) -> str:
        """Prefer an explicit merchant line, else fall back to the sender."""
        for line in self.body_text.splitlines():
            lowered = line.lower().strip()
            for prefix in MERCHANT_PREFIXES:
                if lowered.startswith(prefix):
                    value = (
                        line.split(":", 1)[-1] if ":" in line else line[len(prefix) :]
                    )
                    value = _trim_address(" ".join(value.split()).strip(" -–—:"))
                    if len(value) >= 3:
                        return value
        return self.sender_name or self.sender_email

    def as_row(self) -> dict[str, str | None]:
        return {
            "source_kind": "email",
            "email_message_id": self.message_id or None,
            "email_subject": self.subject or None,
            "email_from": (
                f"{self.sender_name} <{self.sender_email}>".strip()
                if self.sender_email
                else self.sender_name or None
            ),
            "email_date": self.date.isoformat() if self.date else None,
        }


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return " ".join(str(make_header(decode_header(raw))).split())
    except Exception:
        return " ".join(str(raw).split())


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def structured_rows(html: str) -> list[list[str]]:
    """Reduce HTML to rows of cells, keeping each table row intact.

    HTML receipts lay a label and its amount out in sibling cells, so the cell
    boundaries are the structure worth keeping: they are what lets the renderer
    put a label and its amount on one visual line, and what stops field
    extraction from reading "Total" and "44.000" as unrelated lines.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")

    rows: list[list[str]] = []
    for row in soup.find_all("tr"):
        if row.find("tr"):
            continue  # only the innermost rows of nested layout tables
        cells = [
            " ".join(cell.get_text(" ", strip=True).split())
            for cell in row.find_all(["td", "th"])
        ]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)

    if len(rows) < 3:
        # Not a table-based layout; fall back to one cell per line of block text.
        rows = [[" ".join(line.split())] for line in soup.get_text("\n").splitlines()]

    return [row for row in rows if any(cell.strip() for cell in row)]


def html_to_text(html: str) -> str:
    """Flatten HTML into lines, keeping each table row on one line."""
    lines = ["   ".join(row).strip() for row in structured_rows(html)]
    return "\n".join(line for line in lines if line)


def _safe_name(value: str) -> str:
    return (_UNSAFE.sub("_", value).strip("._") or "attachment")[:60]


def _extract_parts(
    message: Message, index: int, attachment_dir: Path, min_bytes: int
) -> tuple[str, str, list[Path]]:
    plain: list[str] = []
    html: list[str] = []
    images: list[Path] = []
    counter = 0

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        filename = part.get_filename()

        if content_type.startswith("image/"):
            payload = part.get_payload(decode=True)
            if not payload or len(payload) < min_bytes:
                continue  # logo, spacer or tracking pixel
            counter += 1
            suffix = Path(filename).suffix if filename else ""
            if not suffix:
                suffix = "." + (content_type.split("/", 1)[1].split("+")[0] or "jpg")
            stem = _safe_name(Path(filename).stem if filename else f"image{counter}")
            target = (
                attachment_dir / f"msg{index:04d}_{counter:02d}_{stem}{suffix.lower()}"
            )
            attachment_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            images.append(target)
            continue

        if content_type == "text/plain":
            plain.append(_part_text(part))
        elif content_type == "text/html":
            html.append(_part_text(part))

    markup = "\n".join(chunk for chunk in html if chunk.strip()).strip()
    body = "\n".join(chunk for chunk in plain if chunk.strip()).strip()
    if not body and markup:
        body = html_to_text(markup)
    return body, markup, images


def iter_mbox(
    path: Path, attachment_dir: Path, min_attachment_bytes: int = MIN_ATTACHMENT_BYTES
) -> Iterator[EmailReceipt]:
    """Yield one EmailReceipt per message, extracting image parts to disk."""
    path = Path(path)
    if not path.is_file():
        raise MailboxError(f"Mailbox not found: {path}")

    try:
        box = mailbox.mbox(str(path))
    except Exception as exc:
        raise MailboxError(f"Could not open {path.name}: {exc}") from exc

    try:
        for index, message in enumerate(box):
            try:
                body, markup, images = _extract_parts(
                    message, index, Path(attachment_dir), min_attachment_bytes
                )
            except Exception as exc:  # one broken message must not stop the import
                body, markup, images = f"[could not read message: {exc}]", "", []

            sender_name, sender_email = parseaddr(message.get("From") or "")
            date = None
            if message.get("Date"):
                try:
                    date = parsedate_to_datetime(message.get("Date"))
                except (TypeError, ValueError):
                    date = None

            yield EmailReceipt(
                index=index,
                mbox_path=path,
                subject=_decode(message.get("Subject")),
                sender_name=_decode(sender_name) or sender_email,
                sender_email=sender_email,
                message_id=(message.get("Message-ID") or "").strip(),
                date=date,
                body_text=body,
                body_html=markup,
                images=images,
            )
    finally:
        try:
            box.close()
        except Exception:
            pass


def count_messages(path: Path) -> int:
    box = mailbox.mbox(str(path))
    try:
        return len(box)
    finally:
        box.close()
