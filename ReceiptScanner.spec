# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: bundles OpenCV, the Tesseract binary and the Indonesian
language model so the built app runs on a machine with none of them installed.

Build with:  pyinstaller --noconfirm ReceiptScanner.spec
"""

import shutil
import subprocess
import sys
from pathlib import Path

BUNDLE_LANGUAGES = ("ind", "eng")  # Indonesian is the default; English is a fallback.


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


TESSERACT = find_tesseract()
TESSDATA = find_tessdata(TESSERACT, BUNDLE_LANGUAGES)

print(f"[spec] tesseract: {TESSERACT}")
print(f"[spec] tessdata:  {TESSDATA} -> {', '.join(BUNDLE_LANGUAGES)}")

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
    icon=None,
    bundle_identifier="local.receiptscanner.app",
    info_plist={
        "CFBundleName": "Receipt Scanner",
        "CFBundleDisplayName": "Receipt Scanner",
        "CFBundleShortVersionString": "1.1.0",
        "CFBundleVersion": "1.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Local receipt scanning with OpenCV and Tesseract.",
    },
)
