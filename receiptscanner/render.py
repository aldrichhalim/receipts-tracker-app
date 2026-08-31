"""Drawing an HTML e-receipt as an image so it can be scanned like a photo.

`mail.structured_rows` reduces the markup to rows of cells; this module lays
those rows out on a white canvas and hands back a grayscale array that goes
through OCR exactly as a photographed receipt would.

Two things this buys over reading the message body as text:

* every e-receipt gets a picture in the review pane, so a human can check the
  suggested figures against something instead of taking them on trust,
* a label and its amount end up on one *visual* line even when the markup put
  them in cells a line-oriented reader would never have joined.

The cost is that OCR re-reads text we already had in hand, so anything it
mis-reads is noise we introduced. That trade was made deliberately.

Nothing here reaches the network: it draws the markup that was already in the
mailbox, and remote `<img src="https://…">` were never fetched in the first
place.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .mail import EmailReceipt, structured_rows
from .resources import find_render_font

# Mirrors config.BUILTIN_DEFAULTS["email_render"]; used when a caller passes a
# partial options dict.
DEFAULTS: dict[str, Any] = {
    "width": 1600,
    "font_size": 26,
    "header_font_size": 34,
    "line_spacing": 12,
    "margin": 48,
    "column_gap": 80,
    "max_height": 20000,
    "font_path": "",
    "psm": 4,
}


class RenderError(RuntimeError):
    pass


# Characters a text face has no glyph for, which Pillow draws as tofu boxes and
# Tesseract then reads as invented words. Receipts are Latin script, so emoji,
# dingbats, arrows and private-use icon fonts are all noise.
_UNDRAWABLE = re.compile(
    "["
    "\u200b-\u200f\ufeff"  # zero-width joiners and marks
    "\u2190-\u2bff"  # arrows, geometric shapes, dingbats
    "\ufe00-\ufe0f"  # variation selectors
    "\ue000-\uf8ff"  # private use: icon fonts
    "\U0001f000-\U0001faff"  # emoji
    "]"
)


def _sanitize(text: str) -> str:
    return " ".join(_UNDRAWABLE.sub(" ", text).split())


def _load_font(path_hint: str, size: int) -> ImageFont.ImageFont:
    found = find_render_font(path_hint)
    if found is not None:
        try:
            return ImageFont.truetype(str(found), size)
        except OSError:
            pass
    # Pillow's built-in face is a fixed small bitmap: legible, but OCR reads it
    # poorly. Better than refusing to render.
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, limit: int) -> list[str]:
    """Greedy word wrap to `limit` pixels."""
    if limit <= 0 or not text:
        return [text] if text else []

    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and _text_width(draw, candidate, font) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [text]


def _dedupe(rows: Sequence[list[str]]) -> list[list[str]]:
    """Drop runs of identical rows.

    Email templates repeat spacer rows, and a wall of the same string teaches
    Tesseract nothing while making the canvas taller.
    """
    out: list[list[str]] = []
    for row in rows:
        if out and out[-1] == row:
            continue
        out.append(row)
    return out


def _header_lines(receipt: EmailReceipt) -> list[str]:
    """Subject, sender and date, so the render carries them even if the body
    never prints them. `parse_receipt` reads the date off this."""
    lines = []
    if receipt.subject:
        lines.append(_sanitize(receipt.subject))
    sender = _sanitize(receipt.sender_name or receipt.sender_email)
    if sender:
        lines.append(f"From: {sender}")
    if receipt.date is not None:
        lines.append(f"Date: {receipt.date.strftime('%d/%m/%Y %H:%M')}")
    return lines


def _layout(
    draw: ImageDraw.ImageDraw,
    rows: Sequence[list[str]],
    header: Sequence[str],
    body_font,
    header_font,
    options: dict[str, Any],
) -> list[tuple[int, int, str, Any]]:
    """Measure pass: resolve every string to an (x, y, text, font) placement.

    Runs before the canvas exists so the height can be sized to the content
    rather than guessed and cropped.
    """
    width = int(options["width"])
    margin = int(options["margin"])
    spacing = int(options["line_spacing"])
    gap = int(options["column_gap"])
    content = max(1, width - 2 * margin)

    placements: list[tuple[int, int, str, Any]] = []
    y = margin

    def line_height(font) -> int:
        ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (11, 3)
        return ascent + descent + spacing

    for line in header:
        for wrapped in _wrap(draw, line, header_font, content):
            placements.append((margin, y, wrapped, header_font))
            y += line_height(header_font)
    if header:
        y += spacing * 2  # breathing room before the receipt proper

    step = line_height(body_font)
    for row in rows:
        if len(row) == 1:
            wrapped = _wrap(draw, row[0], body_font, content)
            for line in wrapped:
                placements.append((margin, y, line, body_font))
                y += step

        elif len(row) == 2:
            # The case worth rendering: label flush left, value flush right, so
            # the two share a baseline no matter how the markup nested them.
            label, value = row
            value_width = _text_width(draw, value, body_font)
            label_limit = max(1, content - value_width - gap)
            label_lines = _wrap(draw, label, body_font, label_limit)
            placements.append((width - margin - value_width, y, value, body_font))
            for line in label_lines:
                placements.append((margin, y, line, body_font))
                y += step

        else:
            column = max(1, (content - gap * (len(row) - 1)) // len(row))
            wrapped = [_wrap(draw, cell, body_font, column) for cell in row]
            for index, lines in enumerate(wrapped):
                x = margin + index * (column + gap)
                for offset, line in enumerate(lines):
                    placements.append((x, y + offset * step, line, body_font))
            y += step * max(len(lines) for lines in wrapped)

    return placements


def render_email(receipt: EmailReceipt, options: dict[str, Any]) -> np.ndarray:
    """Draw an e-receipt as a grayscale image ready for OCR.

    Raises RenderError when the message has nothing to draw.
    """
    settings = {**DEFAULTS, **(options or {})}

    markup = receipt.body_html.strip()
    if markup:
        rows = structured_rows(markup)
    else:
        # No HTML part at all: the plain-text body is still worth drawing.
        rows = [[line] for line in receipt.body_text.splitlines() if line.strip()]

    cleaned = []
    for row in rows:
        cells = [text for text in (_sanitize(cell) for cell in row) if text]
        if cells:
            cleaned.append(cells)
    rows = _dedupe(cleaned)

    header = [line for line in _header_lines(receipt) if line.strip()]
    if not rows and not header:
        raise RenderError("Message has no renderable content")

    width = max(320, int(settings["width"]))
    margin = int(settings["margin"])
    font_hint = str(settings.get("font_path") or "")
    body_font = _load_font(font_hint, int(settings["font_size"]))
    header_font = _load_font(font_hint, int(settings["header_font_size"]))

    # Measuring needs a draw context; a 1x1 scratch image is enough for that.
    scratch = ImageDraw.Draw(Image.new("L", (1, 1), 255))
    placements = _layout(scratch, rows, header, body_font, header_font, settings)
    if not placements:
        raise RenderError("Message has no renderable content")

    step = int(settings["font_size"]) + int(settings["line_spacing"])
    height = min(
        int(settings["max_height"]),
        max(placements[-1][1] + step + margin, 2 * margin + step),
    )

    canvas = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(canvas)
    for x, y, text, font in placements:
        if y >= height:
            break  # clamped by max_height
        draw.text((x, y), text, font=font, fill=0)

    return np.array(canvas)
