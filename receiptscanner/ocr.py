"""Tesseract wrapper. Runs entirely locally against a bundled or system binary."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytesseract
from pytesseract import Output

from .imaging import to_pil
from .resources import find_tessdata_dir, find_tesseract_binary


class OcrUnavailableError(RuntimeError):
    pass


class OcrError(RuntimeError):
    pass


@dataclass
class OcrResult:
    text: str
    mean_confidence: float
    word_count: int
    lang: str


_engine: "TesseractEngine | None" = None


class TesseractEngine:
    def __init__(self) -> None:
        binary = find_tesseract_binary()
        if binary is None:
            raise OcrUnavailableError(
                "Tesseract was not found.\n\n"
                "Install it with:  brew install tesseract tesseract-lang\n"
                "or point the TESSERACT_CMD environment variable at the binary."
            )
        self.binary = binary
        self.tessdata_dir = find_tessdata_dir(binary)

        pytesseract.pytesseract.tesseract_cmd = str(binary)
        if self.tessdata_dir is not None:
            # Tesseract 5 reads TESSDATA_PREFIX as the tessdata directory itself.
            os.environ["TESSDATA_PREFIX"] = str(self.tessdata_dir)

        try:
            self.version = str(pytesseract.get_tesseract_version())
        except Exception as exc:
            raise OcrUnavailableError(
                f"Tesseract at {binary} did not run: {exc}"
            ) from exc

    def available_languages(self) -> list[str]:
        if self.tessdata_dir is not None:
            return sorted(p.stem for p in self.tessdata_dir.glob("*.traineddata"))
        try:
            return sorted(pytesseract.get_languages(config=""))
        except Exception:
            return []

    def _config(self, options: dict[str, Any]) -> str:
        parts = [
            f"--oem {int(options.get('oem', 3))}",
            f"--psm {int(options.get('psm', 6))}",
        ]
        if self.tessdata_dir is not None:
            parts.append(f'--tessdata-dir "{self.tessdata_dir}"')
        extra = str(options.get("extra_config", "") or "").strip()
        if extra:
            parts.append(extra)
        return " ".join(parts)

    def run(self, image: np.ndarray, options: dict[str, Any]) -> OcrResult:
        lang = str(options.get("lang", "ind") or "ind")
        available = self.available_languages()
        if available:
            missing = [p for p in lang.split("+") if p not in available]
            if missing:
                raise OcrError(
                    f"Language model {'+'.join(missing)!r} is not installed. "
                    f"Available: {', '.join(available) or 'none'}.\n"
                    "Install the Indonesian model with:  brew install tesseract-lang"
                )

        timeout = int(options.get("timeout_seconds", 120) or 0)
        try:
            data = pytesseract.image_to_data(
                to_pil(image),
                lang=lang,
                config=self._config(options),
                output_type=Output.DICT,
                timeout=timeout or 0,
            )
        except RuntimeError as exc:  # pytesseract raises this on timeout
            raise OcrError(f"OCR timed out after {timeout}s") from exc
        except pytesseract.TesseractError as exc:
            raise OcrError(f"Tesseract failed: {exc}") from exc

        return OcrResult(
            text=_rebuild_text(data),
            mean_confidence=_mean_confidence(data),
            word_count=_word_count(data),
            lang=lang,
        )


def _word_count(data: dict[str, list[Any]]) -> int:
    return sum(1 for word in data.get("text", []) if str(word).strip())


def _mean_confidence(data: dict[str, list[Any]]) -> float:
    scores = [
        float(conf)
        for conf, word in zip(data.get("conf", []), data.get("text", []))
        if str(word).strip() and float(conf) >= 0
    ]
    return round(sum(scores) / len(scores), 2) if scores else 0.0


def _rebuild_text(data: dict[str, list[Any]]) -> str:
    """Reassemble lines from word boxes.

    image_to_data gives layout and confidence in a single Tesseract invocation;
    rebuilding here avoids a second pass just to get the plain text.
    """
    lines: dict[tuple[int, int, int, int], list[tuple[int, str]]] = {}
    words = data.get("text", [])
    for index, word in enumerate(words):
        text = str(word).strip()
        if not text:
            continue
        key = (
            int(data["page_num"][index]),
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        lines.setdefault(key, []).append((int(data["word_num"][index]), text))

    rendered = []
    for key in sorted(lines):
        ordered = [text for _, text in sorted(lines[key])]
        rendered.append(" ".join(ordered))
    return "\n".join(rendered).strip()


def get_engine() -> TesseractEngine:
    global _engine
    if _engine is None:
        _engine = TesseractEngine()
    return _engine


def describe_engine() -> str:
    try:
        engine = get_engine()
    except OcrUnavailableError as exc:
        return str(exc)
    langs = ", ".join(engine.available_languages()) or "none"
    return (
        f"Tesseract {engine.version}\n"
        f"Binary: {engine.binary}\n"
        f"Tessdata: {engine.tessdata_dir or 'default'}\n"
        f"Languages: {langs}"
    )


def run_ocr(image: np.ndarray, options: dict[str, Any]) -> OcrResult:
    return get_engine().run(image, options)


def tessdata_location() -> Path | None:
    try:
        return get_engine().tessdata_dir
    except OcrUnavailableError:
        return None
