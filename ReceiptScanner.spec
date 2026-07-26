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


def find_tessdata(binary):
    """Ask tesseract itself where its models live, then fall back to guessing."""
    try:
        output = subprocess.run(
            [str(binary), "--list-langs"], capture_output=True, text=True, timeout=30
        ).stderr
        first = output.splitlines()[0] if output else ""
        if '"' in first:
            candidate = Path(first.split('"')[1])
            if candidate.is_dir():
                return candidate
    except Exception:
        pass

    for candidate in (binary.parent.parent / "share" / "tessdata",
                      Path("/opt/homebrew/share/tessdata"),
                      Path("/usr/local/share/tessdata"),
                      Path("/usr/share/tessdata")):
        if candidate.is_dir() and any(candidate.glob("*.traineddata")):
            return candidate
    raise SystemExit("Could not locate the tessdata directory.")


TESSERACT = find_tesseract()
TESSDATA = find_tessdata(TESSERACT)

missing = [lang for lang in BUNDLE_LANGUAGES if not (TESSDATA / f"{lang}.traineddata").is_file()]
if missing:
    raise SystemExit(
        f"Missing language model(s) {', '.join(missing)} in {TESSDATA}.\n"
        "Install them with:  brew install tesseract-lang"
    )

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
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Local receipt scanning with OpenCV and Tesseract.",
    },
)
