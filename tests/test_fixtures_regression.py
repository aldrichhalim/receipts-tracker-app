"""Regression check against the private fixtures in data/.

data/ is gitignored (real names, card digits, tax IDs), so the expected values
live in data/golden.json, which tests/make_golden.py writes locally. Run it
BEFORE changing extraction logic, then `pytest -m fixtures` afterwards.

A difference is not automatically wrong: an intended improvement (for example
USD receipts that used to parse as garbage) legitimately changes values. Each
diff is reported per receipt so it can be judged, and the golden file is
regenerated once the change is accepted.
"""

import json

import pytest

from conftest import DATA_DIR, GOLDEN

pytestmark = pytest.mark.fixtures

FIELDS = ("entry_date", "category", "name", "amount", "currency")


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text())


@pytest.fixture(scope="module")
def current(tmp_path_factory):
    """Run the real pipeline over data/ in a sandbox, exactly as make_golden does."""
    import importlib.util
    import os

    sandbox = tmp_path_factory.mktemp("fixtures")
    config = sandbox / "config.json"
    config.write_text(
        json.dumps(
            {
                "output_dir": str(sandbox / "scans"),
                "originals_dir": str(sandbox / "originals"),
                "database_path": str(sandbox / "receipts.db"),
            }
        )
    )
    previous = {k: os.environ.get(k) for k in ("HOME", "RECEIPTSCANNER_CONFIG")}
    os.environ["HOME"] = str(sandbox)
    os.environ["RECEIPTSCANNER_CONFIG"] = str(config)
    try:
        spec = importlib.util.spec_from_file_location(
            "make_golden", DATA_DIR.parent / "tests" / "make_golden.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.extract_all(DATA_DIR)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_every_golden_receipt_is_still_processed(golden, current):
    missing = sorted(set(golden) - set(current))
    assert not missing, f"receipts no longer produced: {missing[:10]}"


def test_no_receipt_now_fails(current):
    failed = {k: v["error"] for k, v in current.items() if "error" in v}
    assert not failed, failed


def test_extracted_fields_match_the_golden_file(golden, current):
    diffs = []
    for key, expected in golden.items():
        actual = current.get(key, {})
        for field in FIELDS:
            if actual.get(field) != expected.get(field):
                diffs.append(
                    f"{key} {field}: {expected.get(field)!r} -> {actual.get(field)!r}"
                )
    assert not diffs, f"{len(diffs)} field(s) changed:\n" + "\n".join(diffs[:40])
