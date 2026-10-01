"""Shared fixtures.

Every test runs against a throwaway HOME and config, so nothing here can reach
~/Documents/ReceiptScanner. That is not paranoia: an unisolated verification
script once wrote a row and several scans into the real database.
"""

from __future__ import annotations

import copy
import gc
import json
import shutil
from pathlib import Path

import pytest

from receiptscanner.config import BUILTIN_DEFAULTS, Config
from receiptscanner.db import ReceiptStore

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
GOLDEN = DATA_DIR / "golden.json"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point HOME and the config file at tmp_path before anything resolves `~`."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RECEIPTSCANNER_CONFIG", str(tmp_path / "config.json"))
    return home


@pytest.fixture(autouse=True)
def collect_tk_garbage(request):
    """Run the collector on the main thread around every `gui` test.

    A destroyed window's Tk variables are finalised by whichever thread the
    collector happens to run on. On a worker thread that call waits for the main
    thread to be in mainloop, which a test that pumps `update()` never is: it
    stalls for a second per variable, or raises "main thread is not in main loop".
    Collecting here keeps that garbage off the worker.
    """
    if "gui" not in request.keywords:
        yield
        return
    gc.collect()
    yield
    gc.collect()


@pytest.fixture
def categories() -> list[str]:
    return list(BUILTIN_DEFAULTS["categories"])


@pytest.fixture
def config(tmp_path) -> Config:
    """A Config whose every output path lives under tmp_path."""
    data = copy.deepcopy(BUILTIN_DEFAULTS)
    data["output_dir"] = str(tmp_path / "scans")
    data["originals_dir"] = str(tmp_path / "originals")
    data["database_path"] = str(tmp_path / "receipts.db")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return Config(data, path)


@pytest.fixture
def store(tmp_path):
    receipts = ReceiptStore(tmp_path / "receipts.db")
    yield receipts
    receipts.close()


# -- marker auto-skips --------------------------------------------------------
def _tesseract_available() -> bool:
    from receiptscanner.resources import find_tesseract_binary

    return find_tesseract_binary() is not None


def _display_available() -> bool:
    """Probe in a subprocess: a second Tk root in this process segfaults on macOS,
    so the pytest process itself must never create one just to check."""
    import subprocess
    import sys

    try:
        result = subprocess.run(
            [sys.executable, "-c", "import tkinter; tkinter.Tk().destroy()"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def pytest_collection_modifyitems(config, items):
    checks = {
        "ocr": (_tesseract_available, "Tesseract is not installed"),
        "fixtures": (
            lambda: GOLDEN.is_file(),
            "data/golden.json is missing (run tests/make_golden.py)",
        ),
        "gui": (_display_available, "no display available for tkinter"),
    }
    cache: dict[str, bool] = {}
    for item in items:
        for marker, (probe, reason) in checks.items():
            if marker in item.keywords:
                if marker not in cache:
                    cache[marker] = probe()
                if not cache[marker]:
                    item.add_marker(pytest.mark.skip(reason=reason))
