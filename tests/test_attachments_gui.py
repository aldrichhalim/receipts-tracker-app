"""Both report dialogs, driven through the real window (needs a display).

The CSV dialog is checked here too, because the attachment dialog is a subclass of
it and the CSV export must behave exactly as before.
"""

import time
import types

import pytest

from helpers import make_record
from receiptscanner import attachments, report
from test_attachments import scan_png

pytestmark = pytest.mark.gui


@pytest.fixture
def app(config, store, monkeypatch, tmp_path):
    from receiptscanner import app as appmod

    window = appmod.ReceiptScannerApp(config, store)
    window.withdraw()
    window.errors = []
    window.save_target = None
    window.revealed = []
    monkeypatch.setattr(window, "_reveal", lambda path: window.revealed.append(path))
    monkeypatch.setattr(
        appmod,
        "messagebox",
        types.SimpleNamespace(
            showerror=lambda title, message, **k: window.errors.append(
                (title, message)
            ),
            showinfo=lambda *a, **k: None,
            showwarning=lambda *a, **k: None,
            askyesno=lambda *a, **k: True,
        ),
    )
    monkeypatch.setattr(
        appmod,
        "filedialog",
        types.SimpleNamespace(
            asksaveasfilename=lambda **k: str(window.save_target or ""),
        ),
    )
    yield window
    window.destroy()


@pytest.fixture
def saved(store, tmp_path):
    """Three July entries, each with a scan on disk."""
    for i in range(3):
        store.save(
            make_record(
                name=f"Shop {i}",
                entry_date=f"2026-07-0{i + 1}",
                amount=1000.0 * (i + 1),
                scanned_path=str(scan_png(tmp_path / "scans" / f"{i}.png")),
            )
        )
    return 3


def pump(window, until, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        window.update()
        if until():
            return True
        time.sleep(0.01)
    return False


def open_dialog(appmod_class, app, store, config):
    dialog = appmod_class(app, store, config)
    dialog._apply_range("2026-07-01", "2026-07-31")
    return dialog


def exists(dialog) -> bool:
    try:
        return bool(dialog.winfo_exists())
    except Exception:
        return False


class TestCsvDialogIsUnchanged:
    def test_it_still_writes_exactly_what_write_csv_writes(
        self, app, store, config, saved, tmp_path
    ):
        from receiptscanner import app as appmod

        dialog = open_dialog(appmod.ReportDialog, app, store, config)
        app.save_target = tmp_path / "out.csv"
        dialog._export()

        expected = tmp_path / "expected.csv"
        report.write_csv(
            store.entries_between("2026-07-01", "2026-07-31"), expected, config.currency
        )
        assert app.save_target.read_bytes() == expected.read_bytes()
        assert not exists(dialog)
        assert app.revealed == [app.save_target]

    def test_it_has_no_extra_preview_line(self, app, store, config, saved):
        from receiptscanner import app as appmod

        dialog = open_dialog(appmod.ReportDialog, app, store, config)
        assert "\n" not in dialog.preview_label.cget("text")
        assert dialog.export_button.cget("text") == "Export CSV…"
        dialog.destroy()


class TestAttachmentsDialog:
    def test_the_preview_says_where_the_pictures_come_from(
        self, app, store, config, saved
    ):
        from receiptscanner import app as appmod

        dialog = open_dialog(appmod.AttachmentsDialog, app, store, config)
        text = dialog.preview_label.cget("text")
        assert text.splitlines()[0].startswith("3 entries")
        assert "3 with a scan" in text
        assert dialog.export_button.cget("text") == "Export PDF…"
        dialog.destroy()

    def test_it_writes_the_pdf_and_reports_it(
        self, app, store, config, saved, tmp_path
    ):
        from receiptscanner import app as appmod

        dialog = open_dialog(appmod.AttachmentsDialog, app, store, config)
        app.save_target = tmp_path / "out" / "attachments.pdf"
        dialog._export()

        closed = pump(app, lambda: not exists(dialog))
        assert closed, f"dialog never closed; errors={app.errors} busy={dialog._busy}"
        assert app.errors == []
        pdf = app.save_target.read_bytes()
        assert pdf.startswith(b"%PDF")
        assert app.revealed == [app.save_target]
        assert "Attachments saved" in app.status_label.cget("text")

    def test_choosing_no_file_writes_nothing_and_keeps_the_dialog(
        self, app, store, config, saved, tmp_path
    ):
        from receiptscanner import app as appmod

        dialog = open_dialog(appmod.AttachmentsDialog, app, store, config)
        app.save_target = None
        dialog._export()
        assert exists(dialog)
        assert not dialog._busy
        dialog.destroy()

    def test_cancelling_mid_export_closes_the_dialog_and_writes_nothing(
        self, app, store, config, saved, tmp_path, monkeypatch
    ):
        from receiptscanner import app as appmod

        started = []

        def slow(rows, path, config, *, start, end, progress=None, cancel=None):
            started.append(True)
            while not cancel.is_set():
                time.sleep(0.01)
            raise attachments.Cancelled()

        monkeypatch.setattr(appmod.attachments, "write_pdf", slow)
        dialog = open_dialog(appmod.AttachmentsDialog, app, store, config)
        app.save_target = tmp_path / "never.pdf"
        dialog._export()
        assert pump(app, lambda: started)
        assert dialog._busy

        dialog._cancel()
        assert pump(app, lambda: not exists(dialog)), "dialog never closed"
        assert not app.save_target.exists()
        assert app.revealed == []

    def test_the_window_close_button_cancels_rather_than_orphaning_the_worker(
        self, app, store, config, saved, tmp_path, monkeypatch
    ):
        from receiptscanner import app as appmod

        started = []

        def slow(rows, path, config, *, start, end, progress=None, cancel=None):
            started.append(True)
            while not cancel.is_set():
                time.sleep(0.01)
            raise attachments.Cancelled()

        monkeypatch.setattr(appmod.attachments, "write_pdf", slow)
        dialog = open_dialog(appmod.AttachmentsDialog, app, store, config)
        app.save_target = tmp_path / "never.pdf"
        dialog._export()
        assert pump(app, lambda: started)

        # Exactly what the window manager runs when the close button is clicked.
        dialog.tk.call(dialog.protocol("WM_DELETE_WINDOW"))
        assert pump(app, lambda: not exists(dialog))

    def test_a_failure_is_shown_and_the_dialog_stays_usable(
        self, app, store, config, saved, tmp_path, monkeypatch
    ):
        from receiptscanner import app as appmod

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(appmod.attachments, "write_pdf", boom)
        dialog = open_dialog(appmod.AttachmentsDialog, app, store, config)
        app.save_target = tmp_path / "x.pdf"
        dialog._export()

        assert pump(app, lambda: app.errors)
        assert "disk full" in app.errors[0][1]
        assert exists(dialog)
        assert not dialog._busy
        assert dialog.export_button.instate(["!disabled"])
        assert dialog.entry_start.instate(["!disabled"])
        dialog.destroy()

    def test_a_second_export_works_after_a_cancel(
        self, app, store, config, saved, tmp_path
    ):
        from receiptscanner import app as appmod

        first = open_dialog(appmod.AttachmentsDialog, app, store, config)
        first.destroy()

        dialog = open_dialog(appmod.AttachmentsDialog, app, store, config)
        app.save_target = tmp_path / "again.pdf"
        dialog._export()
        assert pump(app, lambda: not exists(dialog))
        assert app.save_target.read_bytes().startswith(b"%PDF")


class TestMenu:
    def test_the_report_menu_offers_both_exports(self, app):
        menubar = app.nametowidget(app.cget("menu"))
        labels = []
        for index in range(menubar.index("end") + 1):
            if (
                menubar.type(index) == "cascade"
                and menubar.entrycget(index, "label") == "Report"
            ):
                submenu = app.nametowidget(menubar.entrycget(index, "menu"))
                labels = [
                    submenu.entrycget(i, "label")
                    for i in range(submenu.index("end") + 1)
                    if submenu.type(i) == "command"
                ]
        assert labels == [
            "Generate Report…",
            "Generate Receipt Attachments (PDF)…",
        ]
