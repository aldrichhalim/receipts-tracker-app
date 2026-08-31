"""Locating files that move around between `python main.py` and a frozen .app bundle."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import APP_NAME


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_roots() -> list[Path]:
    """Directories that may hold bundled data, most specific first.

    PyInstaller lays a macOS .app out as Contents/{MacOS,Frameworks,Resources};
    sys._MEIPASS points at Frameworks and Resources is cross-linked into it, but
    the exact layout has shifted between releases so we probe the siblings too.
    """
    if not is_frozen():
        return [Path(__file__).resolve().parent.parent]

    meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    roots = [meipass, meipass.parent / "Resources", meipass.parent / "Frameworks"]
    seen: list[Path] = []
    for root in roots:
        if root.is_dir() and root not in seen:
            seen.append(root)
    return seen


def find_resource(*relative: str) -> Path | None:
    for root in resource_roots():
        candidate = root.joinpath(*relative)
        if candidate.exists():
            return candidate
    return None


def user_data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


# Homebrew, MacPorts and the usual Linux locations, checked only if nothing is
# bundled and nothing is on PATH.
_FALLBACK_BINARIES = (
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/local/bin/tesseract",
    "/usr/bin/tesseract",
)


def find_tesseract_binary() -> Path | None:
    # PyInstaller flattens collected binaries next to the bundled dylibs, so
    # check the flat layout as well as the tidy prefix-style one.
    for relative in (
        ("tesseract",),
        ("bin", "tesseract"),
        ("tesseract", "bin", "tesseract"),
    ):
        bundled = find_resource(*relative)
        if bundled and bundled.is_file() and os.access(bundled, os.X_OK):
            return bundled

    override = os.environ.get("TESSERACT_CMD")
    if override and Path(override).is_file():
        return Path(override)

    on_path = shutil.which("tesseract")
    if on_path:
        return Path(on_path)

    for candidate in _FALLBACK_BINARIES:
        if Path(candidate).is_file():
            return Path(candidate)
    return None


# Fonts the email renderer draws with, best first. All three ship with macOS;
# the last is the safety net, since /System/Library/Fonts is never trimmed while
# Supplemental/ can be on a minimal install.
_FALLBACK_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def find_render_font(preferred: str = "") -> Path | None:
    """A TrueType face for rendering email receipts.

    Returning None is not fatal: the renderer falls back to Pillow's built-in
    bitmap font, which OCR reads badly but which keeps the app running.
    """
    if preferred:
        candidate = Path(preferred).expanduser()
        if candidate.is_file():
            return candidate

    bundled = find_resource("fonts")
    if bundled and bundled.is_dir():
        for suffix in ("*.ttf", "*.ttc", "*.otf"):
            for candidate in sorted(bundled.glob(suffix)):
                return candidate

    for candidate in _FALLBACK_FONTS:
        if Path(candidate).is_file():
            return Path(candidate)
    return None


def find_tessdata_dir(binary: Path | None = None) -> Path | None:
    """Directory holding the *.traineddata files."""
    bundled = find_resource("tessdata")
    if bundled and any(bundled.glob("*.traineddata")):
        return bundled

    override = os.environ.get("TESSDATA_PREFIX")
    if override:
        candidates = [Path(override), Path(override) / "tessdata"]
        for candidate in candidates:
            if candidate.is_dir() and any(candidate.glob("*.traineddata")):
                return candidate

    # if binary is not None:
    #     # <prefix>/bin/tesseract -> <prefix>/share/tessdata
    #     prefix = binary.resolve().parent.parent
    #     for rel in ("share/tessdata", "tessdata"):
    #         candidate = prefix / rel
    #         if candidate.is_dir() and any(candidate.glob("*.traineddata")):
    #             return candidate

    for candidate in (
        Path("/opt/homebrew/share/tessdata"),
        Path("/usr/local/share/tessdata"),
        Path("/usr/share/tessdata"),
        Path("/usr/share/tesseract-ocr/5/tessdata"),
    ):
        if candidate.is_dir() and any(candidate.glob("*.traineddata")):
            return candidate
    return None
