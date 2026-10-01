"""The guard against tests reaching real user data.

An earlier verification script, run without isolation, wrote a row and several
scans into the real ~/Documents/ReceiptScanner. The autouse fixture in conftest
makes that impossible; these tests prove it keeps doing so.
"""

import os
from pathlib import Path

from receiptscanner.config import Config


def test_home_is_redirected_into_the_sandbox(tmp_path):
    assert Path("~").expanduser().is_relative_to(tmp_path)


def test_default_paths_resolve_inside_the_sandbox(tmp_path):
    config = Config.load()
    for path in (config.output_dir, config.originals_dir, config.database_path):
        assert path.is_relative_to(tmp_path), path


def test_the_config_file_lives_in_the_sandbox(tmp_path):
    config = Config.load()
    assert config.path.is_relative_to(tmp_path)
    assert os.environ["RECEIPTSCANNER_CONFIG"].startswith(str(tmp_path))


def test_no_database_outside_the_sandbox_is_ever_opened(tmp_path, monkeypatch):
    """Spy on every sqlite3.connect: the real receipts.db must never be touched."""
    import sqlite3

    from receiptscanner.db import ReceiptStore

    opened = []
    real_connect = sqlite3.connect

    def spy(database, *args, **kwargs):
        opened.append(Path(str(database)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)

    config = Config.load()
    config.ensure_directories()
    ReceiptStore(config.database_path).close()

    assert opened, "the spy saw no connection, so this test proves nothing"
    assert all(path.is_relative_to(tmp_path) for path in opened), opened
