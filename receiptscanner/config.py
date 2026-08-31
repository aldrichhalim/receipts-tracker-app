"""Configuration loading.

Everything the app writes to disk is addressed through here, so redirecting the
output of a build is a matter of editing one JSON file.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .resources import find_resource, is_frozen, user_data_dir

CONFIG_FILENAME = "config.json"
DEFAULT_CONFIG_FILENAME = "config.default.json"

# Mirrors config.default.json. Kept in code as well so a deleted or truncated
# JSON file degrades to working defaults instead of a crash on startup.
BUILTIN_DEFAULTS: dict[str, Any] = {
    "output_dir": "~/Documents/ReceiptScanner/scans",
    "originals_dir": "~/Documents/ReceiptScanner/originals",
    "copy_originals": False,
    "database_path": "~/Documents/ReceiptScanner/receipts.db",
    "scan_filename_template": "{stem}_{hash8}.png",
    "currency": "IDR",
    "categories": [
        "Makanan & Minuman",
        "Belanja Harian",
        "Transportasi",
        "Kesehatan",
        "Tagihan & Utilitas",
        "Perjalanan & Akomodasi",
        "Hiburan",
        "Perlengkapan Kantor",
        "Elektronik",
        "Lainnya",
    ],
    "ocr": {
        "lang": "ind",
        "psm": 6,
        "oem": 3,
        "extra_config": "",
        "timeout_seconds": 120,
    },
    "preprocess": {
        "detect_document": True,
        "detect_max_dim": 700,
        "min_document_area_ratio": 0.08,
        "min_page_contrast": 4.0,
        "crop_padding": 0.02,
        "mask_background": True,
        "mask_grow_ratio": 0.004,
        "deskew": True,
        "max_deskew_angle": 15.0,
        "target_ocr_width": 1300,
        "shadow_kernel": 31,
        "denoise": "bilateral",
        "clahe_clip": 0.0,
        "clahe_grid": 8,
        "unsharp_amount": 0.8,
        "adaptive_block_size": 51,
        "adaptive_c": 13,
        "min_blob_area": 25,
        "border_px": 24,
    },
    # How an HTML e-receipt is drawn before OCR. psm 4 ("single column of
    # variable-size text") reads a rendered label/amount layout better than the
    # psm 6 used for photographed receipts.
    "email_render": {
        "width": 1600,
        "font_size": 26,
        "header_font_size": 34,
        "line_spacing": 12,
        "margin": 48,
        "column_gap": 80,
        "max_height": 20000,
        "font_path": "",
        "psm": 4,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def config_path() -> Path:
    """Where the editable config lives.

    A checkout keeps it beside the source; a frozen app keeps it in the user's
    data directory, since the bundle itself is read-only on a signed install.
    """
    override = os.environ.get("RECEIPTSCANNER_CONFIG")
    if override:
        return Path(override).expanduser()
    if is_frozen():
        return user_data_dir() / CONFIG_FILENAME
    return Path(__file__).resolve().parent.parent / CONFIG_FILENAME


def _packaged_defaults() -> dict[str, Any]:
    packaged = find_resource(DEFAULT_CONFIG_FILENAME)
    if packaged is not None:
        try:
            with packaged.open(encoding="utf-8") as handle:
                return _deep_merge(BUILTIN_DEFAULTS, json.load(handle))
        except (OSError, json.JSONDecodeError):
            pass
    return copy.deepcopy(BUILTIN_DEFAULTS)


class Config:
    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self._data = data
        self.path = path

    # -- loading ---------------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        defaults = _packaged_defaults()
        data = defaults

        if path.is_file():
            try:
                with path.open(encoding="utf-8") as handle:
                    data = _deep_merge(defaults, json.load(handle))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"Could not read {path}: {exc}") from exc
        else:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8") as handle:
                    json.dump(defaults, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
            except OSError:
                # Read-only location: run from defaults rather than refusing to start.
                pass

        return cls(data, path)

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    # -- resolved paths --------------------------------------------------
    def _path(self, key: str) -> Path:
        raw = str(self.get(key, default=BUILTIN_DEFAULTS[key]))
        return Path(raw).expanduser()

    @property
    def output_dir(self) -> Path:
        return self._path("output_dir")

    @property
    def originals_dir(self) -> Path:
        return self._path("originals_dir")

    @property
    def database_path(self) -> Path:
        return self._path("database_path")

    @property
    def copy_originals(self) -> bool:
        return bool(self.get("copy_originals", default=False))

    @property
    def scan_filename_template(self) -> str:
        return str(self.get("scan_filename_template", default="{stem}_{hash8}.png"))

    @property
    def currency(self) -> str:
        return str(self.get("currency", default="IDR"))

    @property
    def categories(self) -> list[str]:
        cats = self.get("categories", default=[])
        return (
            [str(c) for c in cats]
            if isinstance(cats, list) and cats
            else list(BUILTIN_DEFAULTS["categories"])
        )

    @property
    def ocr(self) -> dict[str, Any]:
        return dict(self.get("ocr", default={}) or {})

    @property
    def preprocess(self) -> dict[str, Any]:
        merged = dict(BUILTIN_DEFAULTS["preprocess"])
        merged.update(self.get("preprocess", default={}) or {})
        return merged

    @property
    def email_render(self) -> dict[str, Any]:
        merged = dict(BUILTIN_DEFAULTS["email_render"])
        merged.update(self.get("email_render", default={}) or {})
        return merged

    def ensure_directories(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.copy_originals:
            self.originals_dir.mkdir(parents=True, exist_ok=True)


class ConfigError(RuntimeError):
    pass
