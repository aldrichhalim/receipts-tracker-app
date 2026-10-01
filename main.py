#!/usr/bin/env python3
"""Entry point for the Narmada desktop app.

Normally launches the GUI. `--self-test [image ...]` instead runs the pipeline
headlessly and prints what it found, which is how a frozen bundle gets checked:
it proves the app can locate its bundled Tesseract binary and language models
from inside the .app, where the paths differ from a source checkout.
"""

from __future__ import annotations

import multiprocessing
import sys


def self_test(paths: list[str]) -> int:
    from pathlib import Path

    from receiptscanner import __version__
    from receiptscanner.config import Config
    from receiptscanner.ocr import describe_engine, get_engine
    from receiptscanner.resources import is_frozen, resource_roots

    print(f"Narmada {__version__}  (frozen={is_frozen()})")
    print("resource roots:")
    for root in resource_roots():
        print(f"  {root}")

    config = Config.load()
    config.ensure_directories()
    print(f"\nconfig:    {config.path}")
    print(f"scans:     {config.output_dir}")
    print(f"database:  {config.database_path}")
    print(f"language:  {config.ocr.get('lang')}")
    print(f"\n{describe_engine()}")

    try:
        get_engine()
    except Exception as exc:
        print(f"\nFAIL: OCR engine unavailable: {exc}")
        return 1

    if not paths:
        print("\nNo images given; engine checks passed.")
        return 0

    from receiptscanner.mail import iter_mbox
    from receiptscanner.pipeline import process_email, process_image

    def report(output, label: str) -> int:
        suggestion = output.suggestion
        if output.rendered:
            print(
                f"  [render] conf={output.ocr.mean_confidence} "
                f"words={output.ocr.word_count}"
            )
            print(f"  render -> {output.scanned_path}")
        elif output.scan is not None:
            print(
                f"  [image] detected={output.scan.document_detected} "
                f"conf={output.ocr.mean_confidence} words={output.ocr.word_count}"
            )
            print(f"  scan -> {output.scanned_path}")
        else:
            print(f"  [email] {output.ocr.word_count} words, OCR skipped (no image)")
        print(
            f"  date={suggestion.entry_date!r} category={suggestion.category!r} "
            f"name={suggestion.name!r} amount={suggestion.amount!r} "
            f"currency={suggestion.currency!r}"
        )
        if output.ocr.word_count == 0:
            print(f"  FAIL: no text recovered from {label}")
            return 1
        return 0

    checked = failures = 0
    for raw in paths:
        path = Path(raw)

        if path.suffix.lower() == ".mbox":
            attachments = config.output_dir.parent / "email_attachments"
            print(f"\n=== {path.name} ===")
            try:
                receipts = list(iter_mbox(path, attachments))
            except Exception as exc:
                print(f"  FAIL: {type(exc).__name__}: {exc}")
                failures += 1
                continue
            for receipt in receipts:
                checked += 1
                print(f"\n--- [{receipt.index}] {receipt.label[:60]} ---")
                try:
                    if receipt.images:
                        for image in receipt.images:
                            failures += report(
                                process_image(image, config, email=receipt), image.name
                            )
                    elif receipt.body_html.strip() or receipt.body_text.strip():
                        failures += report(process_email(receipt, config), path.name)
                    else:
                        print("  (no image and no body; skipped)")
                except Exception as exc:
                    print(f"  FAIL: {type(exc).__name__}: {exc}")
                    failures += 1
            continue

        checked += 1
        print(f"\n--- {path.name} ---")
        try:
            failures += report(process_image(path, config), path.name)
        except Exception as exc:
            print(f"  FAIL: {type(exc).__name__}: {exc}")
            failures += 1

    print(
        f"\n{'FAILED' if failures else 'PASSED'}: {checked - failures}/{checked} receipts"
    )
    return 1 if failures else 0


def main() -> int:
    # Must run before anything spawns a process inside a frozen bundle.
    multiprocessing.freeze_support()

    argv = sys.argv[1:]
    if argv and argv[0] == "--self-test":
        return self_test(argv[1:])

    from receiptscanner.app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
