# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: bundles OpenCV, the Tesseract binary and the Indonesian
language model so the built app runs on a machine with none of them installed.

Build with:  pyinstaller --noconfirm ReceiptScanner.spec
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BUNDLE_LANGUAGES = ("ind", "eng")  # Indonesian is the default; English is a fallback.

ICON_SOURCE = Path(SPECPATH) / "assets" / "logo-app.png"
# macOS draws an icon's artwork inside a transparent margin (Apple's template
# fills 824 of 1024 px). Full-bleed art looks oversized next to every other Dock icon.
ICON_ARTWORK_FRACTION = 824 / 1024


def find_tesseract():
    binary = shutil.which("tesseract")
    if binary is None:
        for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract",
                          "/opt/local/bin/tesseract", "/usr/bin/tesseract"):
            if Path(candidate).is_file():
                binary = candidate
                break
    if binary is None:
        raise SystemExit(
            "tesseract not found on PATH.\n"
            "Install it first:  brew install tesseract tesseract-lang"
        )
    return Path(binary).resolve()


def _tessdata_candidates(binary):
    """Every directory that might hold the .traineddata files, best guess first.

    `tesseract --list-langs` names its own data directory, but writes the list to
    stdout on Tesseract 5 and to stderr on older builds, so read both.
    """
    candidates = []

    try:
        result = subprocess.run(
            [str(binary), "--list-langs"], capture_output=True, text=True, timeout=30
        )
        for stream in (result.stdout, result.stderr):
            first = stream.splitlines()[0] if stream else ""
            if '"' in first:
                candidates.append(Path(first.split('"')[1]))
    except Exception:
        pass

    # Homebrew links the models into <prefix>/share/tessdata but keeps the binary
    # in <prefix>/Cellar/tesseract/<ver>/bin. Resolving the binary therefore walks
    # INTO the Cellar, whose tessdata carries only eng+osd - never the language
    # packs. So derive a prefix from the unresolved path too, and check both.
    for base in (Path(binary), Path(binary).resolve()):
        candidates.append(base.parent.parent / "share" / "tessdata")

    candidates += [
        Path("/opt/homebrew/share/tessdata"),
        Path("/usr/local/share/tessdata"),
        Path("/opt/local/share/tessdata"),
        Path("/usr/share/tessdata"),
        Path("/usr/share/tesseract-ocr/5/tessdata"),
    ]

    seen, unique = set(), []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def find_tessdata(binary, languages):
    """Pick the first candidate that actually holds every model we bundle.

    Accepting a directory just because it exists is how a build ends up shipping
    eng-only Cellar tessdata and failing on its first Indonesian receipt.
    """
    partial = []
    for candidate in _tessdata_candidates(binary):
        if not candidate.is_dir():
            continue
        have = [lang for lang in languages if (candidate / f"{lang}.traineddata").is_file()]
        if len(have) == len(languages):
            return candidate
        if have:
            partial.append(f"{candidate} (has {', '.join(have)})")

    detail = "\n  ".join(partial) if partial else "none of the usual locations"
    raise SystemExit(
        f"Could not find a tessdata directory containing {', '.join(languages)}.\n"
        f"Closest matches:\n  {detail}\n"
        "Install the language models with:  brew install tesseract-lang"
    )


def read_version():
    """The app's version, from the one place it is defined.

    Read as text rather than imported: the spec runs before the package is on
    sys.path, and importing it would drag the whole app in.
    """
    init = Path(SPECPATH) / "receiptscanner" / "__init__.py"
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init.read_text(), re.MULTILINE)
    if match is None:
        raise SystemExit(f"Could not read __version__ from {init}")
    return match.group(1)


def build_icns(source, destination):
    """Turn the square master PNG into a macOS .icns with the standard margin.

    Pillow writes every size from 16 px to 512@2x from the one master, so the
    PNG stays the single source of truth and no generated binary is committed.
    """
    from PIL import Image

    if not source.is_file():
        raise SystemExit(f"App icon not found: {source}")

    size = 1024
    art = Image.open(source).convert("RGBA")
    inset = round(size * ICON_ARTWORK_FRACTION)
    art = art.resize((inset, inset), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(art, ((size - inset) // 2, (size - inset) // 2), art)

    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, format="ICNS")
    return destination


TESSERACT = find_tesseract()
TESSDATA = find_tessdata(TESSERACT, BUNDLE_LANGUAGES)

print(f"[spec] tesseract: {TESSERACT}")
print(f"[spec] tessdata:  {TESSDATA} -> {', '.join(BUNDLE_LANGUAGES)}")

VERSION = read_version()
print(f"[spec] version:   {VERSION}")

ICON = build_icns(ICON_SOURCE, Path(tempfile.mkdtemp(prefix="icon-")) / "icon.icns")
print(f"[spec] icon:      {ICON_SOURCE.name} -> {ICON.name}")

# The binary goes in the root of the collected tree, alongside the dylibs
# PyInstaller pulls in for it, so its rewritten load paths resolve.
binaries = [(str(TESSERACT), ".")]

datas = [(str(TESSDATA / f"{lang}.traineddata"), "tessdata") for lang in BUNDLE_LANGUAGES]
datas.append(("config.default.json", "."))

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=["PIL._tkinter_finder", "pillow_heif", "bs4", "soupsieve",
                   "mailbox", "email.mime"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "pandas", "scipy", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "IPython", "notebook", "pytest", "tkinter.test", "test"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ReceiptScanner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX corrupts signed macOS dylibs
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ReceiptScanner",
)

app = BUNDLE(
    coll,
    name="ReceiptScanner.app",
    icon=str(ICON),
    bundle_identifier="local.receiptscanner.app",
    info_plist={
        "CFBundleName": "Narmada",
        "CFBundleDisplayName": "Narmada",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Narmada: receipt tracker and expense report generator.",
    },
)
