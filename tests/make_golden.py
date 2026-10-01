"""Capture what the pipeline extracts from the private fixtures in data/.

Run this BEFORE changing extraction logic, then `pytest -m fixtures` afterwards:

    .venv/bin/python tests/make_golden.py

The output goes to data/golden.json. data/ is gitignored because the receipts
carry real names, card digits and tax IDs, so the expected values never reach
git. Everything runs in a throwaway directory: nothing is written to
~/Documents/ReceiptScanner.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "data" / "golden.json"
FIELDS = ("entry_date", "category", "name", "amount", "currency")


def sandbox() -> Path:
    """Point HOME and the config at a temp dir before anything imports Config."""
    tmp = Path(tempfile.mkdtemp(prefix="receiptscanner-golden-"))
    config = tmp / "config.json"
    config.write_text(
        json.dumps(
            {
                "output_dir": str(tmp / "scans"),
                "originals_dir": str(tmp / "originals"),
                "database_path": str(tmp / "receipts.db"),
            }
        )
    )
    os.environ["HOME"] = str(tmp)
    os.environ["RECEIPTSCANNER_CONFIG"] = str(config)
    return tmp


def extract_all(data_dir: Path) -> dict[str, dict]:
    sys.path.insert(0, str(ROOT))
    from receiptscanner.config import Config
    from receiptscanner.mail import iter_mbox
    from receiptscanner.pipeline import process_email, process_image

    config = Config.load()
    config.ensure_directories()
    attachments = config.output_dir.parent / "email_attachments"

    def summarise(output) -> dict:
        record = output.as_record(config)
        suggestion = output.suggestion
        return {
            "entry_date": suggestion.entry_date,
            "category": suggestion.category,
            "name": suggestion.name,
            "amount": suggestion.amount,
            "currency": record.get("currency"),
            "words": output.ocr.word_count,
        }

    results: dict[str, dict] = {}
    for path in sorted(data_dir.glob("*.jpeg")) + sorted(data_dir.glob("*.jpg")):
        results[path.name] = summarise(process_image(path, config))

    for mbox in sorted(data_dir.glob("*.mbox")):
        for receipt in iter_mbox(mbox, attachments):
            key = f"{mbox.name}#{receipt.index}"
            try:
                if receipt.images:
                    for n, image in enumerate(receipt.images):
                        results[f"{key}.{n}"] = summarise(
                            process_image(image, config, email=receipt)
                        )
                elif receipt.body_html.strip() or receipt.body_text.strip():
                    results[key] = summarise(process_email(receipt, config))
            except Exception as exc:  # record it, never abort the capture
                results[key] = {"error": f"{type(exc).__name__}: {exc}"}
    return results


def main() -> int:
    data_dir = ROOT / "data"
    if not data_dir.is_dir():
        print("data/ not found: nothing to capture.")
        return 1
    tmp = sandbox()
    results = extract_all(data_dir)
    GOLDEN.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    errors = sum("error" in r for r in results.values())
    print(f"captured {len(results)} receipts ({errors} errors) -> {GOLDEN}")
    print(f"sandbox: {tmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
