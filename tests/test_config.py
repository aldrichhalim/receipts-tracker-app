"""Config: layering, and the duplicated defaults that must never drift."""

import copy
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


class TestCurrencies:
    def make(self, **overrides):
        data = copy.deepcopy(BUILTIN_DEFAULTS)
        data.update(overrides)
        return Config(data, Path("config.json"))

    def test_both_currencies_are_offered_by_default(self):
        assert self.make().currencies == ["IDR", "USD"]

    def test_the_default_currency_is_still_idr(self):
        assert self.make().currency == "IDR"

    def test_a_user_can_extend_the_list(self):
        assert self.make(currencies=["IDR", "USD", "SGD"]).currencies == [
            "IDR",
            "USD",
            "SGD",
        ]

    def test_the_default_currency_is_always_selectable(self):
        # A configured default missing from the list would leave the dropdown
        # unable to show the value the app itself assigned.
        config = self.make(currency="SGD", currencies=["IDR", "USD"])
        assert "SGD" in config.currencies

    def test_a_broken_value_falls_back_to_the_builtin_list(self):
        assert self.make(currencies="IDR").currencies == ["IDR", "USD"]
        assert self.make(currencies=[]).currencies == ["IDR", "USD"]
