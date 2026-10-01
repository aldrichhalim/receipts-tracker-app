"""Config: layering, and the duplicated defaults that must never drift."""

import json
from pathlib import Path

from receiptscanner.config import BUILTIN_DEFAULTS, Config

ROOT = Path(__file__).resolve().parent.parent


def test_the_shipped_json_and_the_builtin_defaults_are_identical():
    """CLAUDE.md: config.default.json is duplicated in code as a last-resort
    fallback, and editing only one of them makes the defaults silently diverge."""
    shipped = json.loads((ROOT / "config.default.json").read_text())
    assert shipped == BUILTIN_DEFAULTS


def test_a_partial_user_file_is_merged_over_the_defaults(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"currency": "USD", "ocr": {"psm": 4}}))
    monkeypatch.setenv("RECEIPTSCANNER_CONFIG", str(path))

    config = Config.load()
    assert config.currency == "USD"
    assert config.ocr["psm"] == 4
    assert config.ocr["lang"] == "ind"  # untouched keys keep their defaults


def test_first_run_writes_a_full_snapshot_of_the_defaults(tmp_path, monkeypatch):
    path = tmp_path / "fresh" / "config.json"
    monkeypatch.setenv("RECEIPTSCANNER_CONFIG", str(path))
    Config.load()
    assert json.loads(path.read_text()) == BUILTIN_DEFAULTS
