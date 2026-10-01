"""Drive the real window headlessly: import a mailbox, process, review, save.

This is the pattern CLAUDE.md describes for integration testing the GUI:
construct the app, withdraw it, stub the dialogs, and pump update() while the
worker thread runs.
"""

import time
import types

import pytest

from helpers import build_mbox, make_message

pytestmark = [pytest.mark.gui, pytest.mark.ocr]

HTML = (
    "<table>"
    "<tr><td>Fare</td><td>{fare}</td></tr>"
    "<tr><td>Platform Fee</td><td>10.500</td></tr>"
    "<tr><td>Total Paid</td><td>{total}</td></tr>"
    "</table>"
)


@pytest.fixture
def app(config, store, monkeypatch, tmp_path):
    from receiptscanner import app as appmod

    window = appmod.ReceiptScannerApp(config, store)
    window.withdraw()

    window.errors = []
    monkeypatch.setattr(
        appmod,
        "messagebox",
        types.SimpleNamespace(
            showerror=lambda title, message: window.errors.append((title, message)),
            showinfo=lambda *a, **k: None,
            showwarning=lambda *a, **k: None,
            askyesno=lambda *a, **k: True,
        ),
    )
    window.mailbox_path = None
    monkeypatch.setattr(
        appmod,
        "filedialog",
        types.SimpleNamespace(
            askopenfilename=lambda **k: str(window.mailbox_path),
            askopenfilenames=lambda **k: (),
            asksaveasfilename=lambda **k: "",
        ),
    )
    yield window
    window.destroy()


def mailbox(tmp_path, *totals):
    messages = [
        make_message(
            subject=f"Receipt {n}",
            sender="Grab <g@grab.com>",
            message_id=f"<msg-{n}@grab>",
            html=HTML.format(fare="1.000", total=total),
        )
        for n, total in enumerate(totals)
    ]
    return build_mbox(tmp_path / "in.mbox", messages)


def process_everything(app, timeout=180):
    app.start_processing()
    deadline = time.time() + timeout
    while app._worker is not None and app._worker.is_alive() and time.time() < deadline:
        app.update()
        time.sleep(0.05)
    for _ in range(40):  # let the final events drain
        app.update()
        time.sleep(0.02)


def test_import_process_review_and_save(app, store, tmp_path):
    app.mailbox_path = mailbox(tmp_path, "44.000", "64.500")
    app.add_mailbox()
    assert len(app.items) == 2 and not app.errors

    process_everything(app)
    for item in app.items:
        assert item.processed is not None, item.error
        assert item.processed.scanned_preview is not None  # an image to review
        assert item.processed.record["source_kind"] == "email_render"

    for index in range(len(app.items)):
        app._select_index(index)
        app.update()  # draws the canvas
        app.save_current()

    saved = store.recent()
    assert len(saved) == 2
    assert sorted(r["amount"] for r in saved) == [44000.0, 64500.0]
    assert {r["source_kind"] for r in saved} == {"email_render"}


USD_HTML = (
    "<table>"
    "<tr><td>Transaksi Kartu Kredit</td><td>BCA</td></tr>"
    "<tr><td>Merchant</td><td>SURFSHARK</td></tr>"
    "<tr><td>Sejumlah</td><td>USD 56,48</td></tr>"
    "</table>"
)


def test_a_dollar_receipt_is_detected_shown_and_saved_as_usd(app, store, tmp_path):
    box = build_mbox(
        tmp_path / "usd.mbox",
        [make_message(subject="Notifikasi", sender="BCA <b@bca.co.id>", html=USD_HTML)],
    )
    app.mailbox_path = box
    app.add_mailbox()
    process_everything(app)

    (item,) = app.items
    assert item.processed is not None, item.error
    app._select_index(0)
    app.update()

    # The dropdown offers both currencies and shows what was detected.
    assert list(app.entry_currency["values"]) == ["IDR", "USD"]
    assert app.var_currency.get() == "USD"
    assert app.var_amount.get() == "56.48"

    app.save_current()
    (row,) = store.recent()
    assert (row["currency"], row["amount"]) == ("USD", 56.48)


def test_the_currency_can_be_corrected_before_saving(app, store, tmp_path):
    app.mailbox_path = mailbox(tmp_path, "44.000")
    app.add_mailbox()
    process_everything(app)
    app._select_index(0)
    app.update()
    assert app.var_currency.get() == "IDR"

    app.var_currency.set("USD")
    app.save_current()
    assert store.recent()[0]["currency"] == "USD"


def test_the_choice_survives_navigating_away_and_back(app, tmp_path):
    app.mailbox_path = mailbox(tmp_path, "44.000", "64.500")
    app.add_mailbox()
    process_everything(app)

    app._select_index(0)
    app.var_currency.set("USD")
    app._select_index(1)
    assert app.var_currency.get() == "IDR"
    app._select_index(0)
    assert app.var_currency.get() == "USD"
