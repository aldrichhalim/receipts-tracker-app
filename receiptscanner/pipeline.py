"""Ties the stages together.

Two entry points, both returning the same `PipelineOutput` so the UI and the
database do not care where a receipt came from:

* `process_image`  — photo: load, scan, save, OCR, suggest fields.
* `process_email`  — HTML e-receipt with no image: the markup is drawn as an
                     image first, then read back by OCR like any other receipt.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2

from . import imaging, render
from .config import Config
from .imaging import ScanResult
from .mail import EmailReceipt
from .ocr import OcrResult, run_ocr
from .parsing import ParsedReceipt, parse_receipt

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class PipelineOutput:
    ocr: OcrResult
    suggestion: ParsedReceipt
    scan: ScanResult | None = None  # None for image-less email receipts
    scanned_path: Path | None = None
    archived_path: Path | None = None
    email: EmailReceipt | None = None
    source_path: Path | None = None
    rendered: bool = False  # image was drawn from the markup, not photographed

    @property
    def has_image(self) -> bool:
        return self.scan is not None

    def as_record(self, config: Config) -> dict[str, Any]:
        """Flatten into the column set ReceiptStore expects."""
        if self.email is None:
            source_kind = "image"
        elif self.rendered:
            source_kind = "email_render"
        else:
            source_kind = "email_image" if self.has_image else "email"

        record: dict[str, Any] = {
            "source_kind": source_kind,
            "source_path": str(self.source_path) if self.source_path else None,
            "source_filename": self.source_path.name if self.source_path else None,
            "scanned_path": str(self.scanned_path) if self.scanned_path else None,
            "archived_path": str(self.archived_path) if self.archived_path else None,
            "ocr_text_raw": self.ocr.text,
            "ocr_lang": self.ocr.lang,
            "ocr_confidence": self.ocr.mean_confidence,
            "ocr_word_count": self.ocr.word_count,
            "currency": config.currency,
        }

        if self.scan is not None:
            height, width = self.scan.scanned.shape[:2]
            record.update(
                {
                    "scanned_width": int(width),
                    "scanned_height": int(height),
                    "document_detected": int(self.scan.document_detected),
                    "deskew_angle": float(self.scan.deskew_angle),
                    **self.scan.source.as_row(),
                }
            )

        if self.email is not None:
            email_row = self.email.as_row()
            email_row.pop(
                "source_kind", None
            )  # set above; distinguishes attached photos
            record.update(email_row)
        return record


def _safe_stem(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip("._")
    return (cleaned or "receipt")[:60]


def build_output_path(source: Path, sha256: str, config: Config) -> Path:
    template = config.scan_filename_template
    now = datetime.now()
    filename = template.format(
        stem=_safe_stem(source.stem),
        name=_safe_stem(source.name),
        hash8=sha256[:8],
        hash=sha256,
        ts=now.strftime("%Y%m%d-%H%M%S"),
        date=now.strftime("%Y-%m-%d"),
    )
    if not Path(filename).suffix:
        filename += ".png"

    target = config.output_dir / filename
    # Same name, different photo: keep both rather than silently overwriting.
    counter = 1
    while target.exists():
        target = (
            config.output_dir
            / f"{Path(filename).stem}-{counter}{Path(filename).suffix}"
        )
        counter += 1
    return target


def archive_original(source: Path, sha256: str, config: Config) -> Path | None:
    if not config.copy_originals:
        return None
    config.originals_dir.mkdir(parents=True, exist_ok=True)
    target = (
        config.originals_dir
        / f"{_safe_stem(source.stem)}_{sha256[:8]}{source.suffix.lower()}"
    )
    if not target.exists():
        shutil.copy2(source, target)
    return target


def process_image(
    path: Path,
    config: Config,
    progress=None,
    email: EmailReceipt | None = None,
) -> PipelineOutput:
    """Run one photo end to end.

    `progress` is an optional callable taking a short status string, so the UI
    can show which stage is running without knowing the pipeline internals.
    `email` is set when the photo came out of a mailbox, and supplies fallbacks
    for fields the receipt itself does not spell out.
    """

    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    report("Loading image")
    bgr, source = imaging.load_image(Path(path))

    report("Detecting page and cleaning up")
    scan = imaging.scan(bgr, source, config.preprocess)

    report("Saving scanned image")
    scanned_path = build_output_path(source.path, source.sha256, config)
    imaging.write_image(scan.scanned, scanned_path)
    archived_path = archive_original(source.path, source.sha256, config)

    report("Running OCR")
    ocr = run_ocr(scan.scanned, config.ocr)

    report("Reading fields")
    fallback_date = ""
    if source.exif_datetime:
        fallback_date = source.exif_datetime.split(" ")[0]
    if email is not None and email.date_iso:
        fallback_date = fallback_date or email.date_iso

    suggestion = parse_receipt(ocr.text, config.categories, fallback_date=fallback_date)
    if email is not None and not suggestion.name:
        suggestion.name = email.merchant_guess()

    return PipelineOutput(
        ocr=ocr,
        suggestion=suggestion,
        scan=scan,
        scanned_path=scanned_path,
        archived_path=archived_path,
        email=email,
        source_path=source.path,
    )


def process_email(
    receipt: EmailReceipt, config: Config, progress=None
) -> PipelineOutput:
    """Draw an image-less e-receipt, then read it back like a photograph.

    The markup is rendered rather than parsed as text because a rendered layout
    keeps a label and its amount on one line even where the HTML never said so.
    `imaging.scan()` is deliberately skipped: a render is already clean black on
    white, so page detection would have no page to find and the threshold chain
    could only damage glyphs that needed no repair.
    """

    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    report("Rendering email")
    rendered = render.render_email(receipt, config.email_render)

    report("Saving rendered image")
    stem = _safe_stem(receipt.subject or receipt.sender_name or "email")
    # Message-ID is the stable identity of an e-receipt; hash it so the filename
    # is the same shape as a photo's and free of the angle brackets it carries.
    digest = hashlib.sha256(
        (receipt.message_id or f"{receipt.mbox_path}#{receipt.index}").encode()
    ).hexdigest()
    scanned_path = build_output_path(
        receipt.mbox_path.with_name(f"{stem}.png"), digest, config
    )
    imaging.write_image(rendered, scanned_path)

    # Re-read what we just wrote purely to get a real SourceInfo — it carries the
    # sha256 and dimensions the record wants, and hand-faking one would drift.
    _, source = imaging.load_image(scanned_path)
    scan = ScanResult(
        original_bgr=cv2.cvtColor(rendered, cv2.COLOR_GRAY2BGR),
        scanned=rendered,
        source=source,
        document_detected=False,
        deskew_angle=0.0,
        stages=["rendered from email markup", "OCR"],
    )

    report("Running OCR")
    options = dict(config.ocr)
    options["psm"] = config.email_render.get("psm", options.get("psm", 6))
    ocr = run_ocr(rendered, options)

    report("Reading fields")
    suggestion = parse_receipt(
        ocr.text, config.categories, fallback_date=receipt.date_iso
    )
    # The message body names its merchant far more reliably than OCR of a render
    # or a header-line guess, so it still wins here.
    merchant = receipt.merchant_guess()
    if merchant:
        suggestion.name = merchant

    return PipelineOutput(
        ocr=ocr,
        suggestion=suggestion,
        scan=scan,
        scanned_path=scanned_path,
        email=receipt,
        source_path=scanned_path,
        rendered=True,
    )
