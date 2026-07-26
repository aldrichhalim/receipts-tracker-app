# Receipt Scanner

A local desktop app for turning photos of receipts into expense entries. Pick
photos, the app straightens and cleans each one into a high-contrast black-and-
white scan, OCRs it with Tesseract's Indonesian model, then walks you through
reviewing every receipt and saves the result to SQLite.

**Nothing leaves the machine — there are no network calls anywhere in the
pipeline**, including for `.mbox` import: remote images referenced by email
HTML are never fetched.

## Features

- **OpenCV preprocessing** that finds the receipt in a photo, straightens it,
  and cleans it into a high-contrast scan — tuned against real photos, not
  synthetic test images (see [The pipeline](#the-pipeline)).
- **Tesseract OCR** with the Indonesian language model, tuned for Indonesian
  number formats (`1.234.567,89`), month names, and point-of-sale vocabulary.
- **Gmail `.mbox` import** that expands a mailbox export into the same review
  queue as photos, reading HTML e-receipts as text directly (no OCR needed)
  and running attached photos through the normal pipeline.
- **A mandatory human review step** before anything is saved — every OCR
  suggestion is editable, never auto-committed.
- **CSV reporting** over a date range, with quick presets and a live
  entry-count/total preview before export.
- **A standalone macOS `.app`** with OpenCV, Tesseract, and the language
  models embedded, built via PyInstaller — no Homebrew required to run it.

## Requirements

- macOS (the build tooling and app bundling target macOS; the Python pipeline
  itself has no macOS-specific code)
- Python 3.11+
- [Homebrew](https://brew.sh) with `tesseract` and `tesseract-lang`, for
  running from source (not needed for the built `.app`, which embeds its own)

## Running from source

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Tesseract and the Indonesian model are needed for a source run (the built app
carries its own copies):

```bash
brew install tesseract tesseract-lang
```

Then:

```bash
.venv/bin/python main.py
```

To exercise the pipeline without the GUI — this is the project's verification
path, and it accepts photos and `.mbox` files alike:

```bash
.venv/bin/python main.py --self-test                     # engine/resource checks only
.venv/bin/python main.py --self-test data/*.jpeg         # receipt photos
.venv/bin/python main.py --self-test data/Unread.mbox    # every receipt in a mailbox
```

`data/` is not tracked in git — real receipts and mailbox exports carry names,
card digits, tax IDs and addresses. Drop your own receipt photos or a `.mbox`
export in there to run the checks above; `--self-test` with no arguments works
on a fresh clone and still verifies that Tesseract and the language models
resolve.

## Using it

1. **Add Images…** (⌘O) — multi-select receipt photos. JPEG, PNG, TIFF, WebP,
   and HEIC if `pillow-heif` is installed.
   **Add from Mailbox…** (⌘M) — import a Gmail `.mbox` export (see below).
2. **Process Queue** (⌘R) — runs the pipeline one item at a time on a
   background thread, so the window stays responsive.
3. **Review every receipt.** The scan and the OCR text sit side by side; the
   entry form below is pre-filled from the OCR. Correct anything, then
   **Save & Next** (⌘S) moves to the next unreviewed receipt. The app will not
   let you quit with receipts still unreviewed without asking.

The OCR panel is editable, and selecting text in it then clicking **Name**,
**Amount**, or **Date** drops that selection into the matching field — handy
when the automatic guess picks the wrong line.

## Reports

**Report → Generate Report…** (⌘E) exports saved entries over a date range to
CSV. The dialog shows how many entries the range covers and what they add up to
before you commit to a file, with quick presets for this month, last month, this
year, and everything on record.

```csv
Date,Category,Expense Detail,Amount (IDR)
2026-06-16,Makanan & Minuman,Caffeine Suite,70000.00
2026-07-14,Transportasi,Grab,44000.00
```

Both ends of the range are inclusive, and the dates may be typed in any format
the entry form accepts (`2026-07-01`, `01/07/2026`); a reversed range is swapped
rather than rejected. Photos and e-receipts appear alike — the report is about
the expense, not where it came from. Entries saved without a date are left out,
since they cannot fall inside a range.

The file is written UTF-8 with a BOM so Excel renders Indonesian text correctly,
and amounts are unformatted so a spreadsheet can sum the column directly. No
total row is appended, which would otherwise get in the way of sorting and
filtering — the total is shown in the app when the report is saved.

## Mailbox import

**Add from Mailbox…** (⌘M) reads a Gmail `.mbox` export and expands it into the
same queue as photos, so everything after import — review, editing, saving — is
identical. A mailbox holds two kinds of receipt, and each takes the path that
suits it:

- **Messages with an attached photo.** Each image is written to
  `<output_dir>/../email_attachments/` and goes through the full OpenCV +
  Tesseract pipeline, unchanged. One queue item per attachment.
- **HTML e-receipts with no image** (Grab, food delivery, airline
  confirmations). The figures are already text in the message body, so they are
  read directly and OCR is skipped — exact, rather than a best guess. These show
  in the queue with a `✉` prefix, and the image pane says so instead of
  displaying a scan.

The body is flattened with each table row kept on one line, because HTML
receipts put a label and its amount in sibling cells and naive tag stripping
separates them, which breaks the line-oriented field extraction.

Two deliberate limits:

- **Remote images are never fetched.** `<img src="https://…">` in a marketing
  email is a logo or a tracking pixel, and this app makes no network calls.
  Only actual attachments are read.
- **Image parts under 8 KB are ignored** as logos, signature images and
  spacers. Tune with `min_attachment_bytes` in `mail.iter_mbox`.

Re-importing the same mailbox flags already-filed messages during processing,
matching on `Message-ID` rather than file hash.

`.mbox` parsing itself needs no third-party library — Python's `mailbox` and
`email` modules handle the format. BeautifulSoup is used only for the
HTML-to-text step.

## Configuration

All paths live in `config.json`, created on first run from `config.default.json`.
Running from source it sits next to the code; in the built app it lives at
`~/Library/Application Support/ReceiptScanner/config.json`. Set the
`RECEIPTSCANNER_CONFIG` environment variable to point somewhere else.

```json
{
  "output_dir": "~/Documents/ReceiptScanner/scans",
  "database_path": "~/Documents/ReceiptScanner/receipts.db",
  "copy_originals": false,
  "currency": "IDR",
  "categories": ["Makanan & Minuman", "Transportasi", "..."],
  "ocr": { "lang": "ind", "psm": 6, "oem": 3 }
}
```

`categories` fills the category dropdown. `ocr.lang` accepts anything installed
(`ind`, `eng`, or `ind+eng`). `preprocess` exposes every pipeline knob; the
defaults were tuned against real receipt photos and are a reasonable starting
point.

The file is written as a complete snapshot of the defaults, so it stays
readable and editable — but that also means a `config.json` from an older
version keeps that version's values. Delete it to regenerate from the current
defaults.

## The pipeline

```
load (EXIF-rotated, HEIC-aware)
  → find the page          brightness/edge/saturation segmentation, scored by
                           how much brighter the region is than its surroundings
  → straighten + crop      minimum-area rectangle, so text is never sheared
  → blank the background   table texture never reaches the threshold step
  → normalise size         downscaling averages away paper grain
  → flatten lighting       divide out the background to erase shadows
  → denoise → sharpen
  → adaptive threshold     → despeckle → pad
  → Tesseract (lang=ind)
  → suggest date / category / name / amount
```

Two choices are worth knowing about, since both are the opposite of the
textbook recipe and both were measured rather than guessed:

- **Detection runs on a ~700px copy.** At full resolution the printed text
  carries more edge energy than the page border, and the outline is never found.
- **The page is straightened with its minimum-area rectangle, not a four-point
  perspective warp.** Corners recovered from an approximated contour are
  imprecise enough that the warp shears the text, costing more accuracy than
  the perspective it corrects.

Measured on the four sample photos in `data/`, by OCR recall of known strings:
recall 0.00 → 0.86, mean confidence 19% → 64%, and spurious "words" per receipt
830 → 160. `--self-test` re-runs this end to end.

### Field extraction

Tuned for Indonesian receipts: `1.234.567,89` numbers, Indonesian month names,
and the usual POS vocabulary. The total is picked by keyword tier
(`GRAND TOTAL` > `TOTAL` > `SUBTOTAL`/`TUNAI`), with the longest matching
keyword deciding the tier so `SUBTOTAL` is never mistaken for the grand total.
Long unformatted digit runs are ignored, which keeps card and merchant numbers
out of the amount field.

Every value is only a suggestion — the review step exists because OCR on a
creased receipt will sometimes be wrong.

## Database

One table, `receipts`, at `database_path`. Alongside your reviewed entry
(`entry_date`, `category`, `name`, `amount`, `currency`, `notes`) each row keeps
the original photo path, the scanned image path, both the raw and the
as-reviewed OCR text, and image details: dimensions, byte size, SHA-256, EXIF
capture time, camera make/model, whether a page was detected, and the deskew
angle. The SHA-256 is indexed, so re-adding a photo you already filed is
flagged during processing.

`source_kind` records where a row came from — `image` (a photo you added),
`email_image` (a photo attached to a message) or `email` (an HTML e-receipt,
which has no `scanned_path` and no image details). Email rows also carry
`email_message_id`, `email_subject`, `email_from` and `email_date`. Databases
created before mailbox support are migrated in place on open; existing rows
default to `source_kind = 'image'`.

```bash
sqlite3 ~/Documents/ReceiptScanner/receipts.db \
  "SELECT entry_date, category, name, amount FROM receipts ORDER BY entry_date;"
```

## Building the app

```bash
.venv/bin/python -m PyInstaller --noconfirm --clean ReceiptScanner.spec
```

Produces `dist/ReceiptScanner.app` (~178 MB) with OpenCV, the Tesseract binary
and its dylibs, and the `ind` + `eng` models all embedded — it runs on a Mac
with neither Homebrew nor Tesseract installed. Edit `BUNDLE_LANGUAGES` in the
spec to change which models ship.

Verify a build without clicking through the GUI:

```bash
./dist/ReceiptScanner.app/Contents/MacOS/ReceiptScanner --self-test data/IMG_5672.jpeg
```

It prints where it resolved its Tesseract binary and models from, then runs the
full pipeline. To prove it is using the *embedded* copies rather than a system
install, run it with Homebrew off the path:

```bash
env -u TESSDATA_PREFIX PATH=/usr/bin:/bin ./dist/ReceiptScanner.app/Contents/MacOS/ReceiptScanner --self-test data/IMG_5672.jpeg
```

The app is unsigned, so the first launch needs right-click → Open (or
`xattr -dr com.apple.quarantine dist/ReceiptScanner.app` after copying it to
another machine).

## Layout

| Path | Purpose |
| --- | --- |
| `main.py` | Entry point; also `--self-test` |
| `receiptscanner/config.py` | Config loading, path resolution |
| `receiptscanner/resources.py` | Finds Tesseract and tessdata, frozen or not |
| `receiptscanner/imaging.py` | OpenCV pipeline |
| `receiptscanner/mail.py` | `.mbox` reading, attachment extraction, HTML → text |
| `receiptscanner/ocr.py` | Tesseract wrapper |
| `receiptscanner/parsing.py` | OCR text → date, category, name, amount |
| `receiptscanner/pipeline.py` | Ties the stages together |
| `receiptscanner/db.py` | SQLite storage |
| `receiptscanner/report.py` | CSV export and date-range presets |
| `receiptscanner/app.py` | tkinter UI |
| `ReceiptScanner.spec` | PyInstaller build |

## License

[MIT](LICENSE)
