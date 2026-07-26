# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local-only tkinter desktop app that turns photos of receipts into expense
entries: OpenCV preprocessing → Tesseract OCR (Indonesian model) → a human
review step → SQLite. There are no network calls anywhere in the pipeline, and
that is deliberate — keep it that way.

## Commands

```bash
# Setup (Python 3.14 venv already present at .venv)
.venv/bin/pip install -r requirements.txt
brew install tesseract tesseract-lang          # needed for source runs only

# Run the GUI
.venv/bin/python main.py

# Verify the pipeline headlessly — this is the closest thing to a test suite
.venv/bin/python main.py --self-test                     # engine/resource checks only
.venv/bin/python main.py --self-test data/IMG_5672.jpeg  # single image
.venv/bin/python main.py --self-test data/*.jpeg         # all four photo fixtures
.venv/bin/python main.py --self-test data/Unread.mbox    # every receipt in a mailbox

# Build the .app (embeds OpenCV, the tesseract binary + dylibs, ind/eng models)
.venv/bin/python -m PyInstaller --noconfirm --clean ReceiptScanner.spec

# Verify a build is genuinely self-contained (Homebrew off the PATH)
env -u TESSDATA_PREFIX PATH=/usr/bin:/bin \
  ./dist/ReceiptScanner.app/Contents/MacOS/ReceiptScanner --self-test data/IMG_5672.jpeg
```

There is no pytest suite. `data/` holds fixtures: four real Indonesian receipt
photos and `Unread.mbox`, a Gmail export of five Grab e-receipts. `--self-test`
runs the full pipeline over whatever you pass and fails if anything yields zero
text. Point `RECEIPTSCANNER_CONFIG` at a scratch JSON file when testing so runs
do not write into `~/Documents/ReceiptScanner`.

The GUI can also be driven headlessly for integration testing: construct
`ReceiptScannerApp`, call `withdraw()`, stub `app.messagebox`/`app.filedialog`,
then pump `app.update()` in a loop while the worker thread runs.

## Architecture

Data flows in one direction through `receiptscanner/`:

```
app.py (tkinter)  ──calls──>  pipeline.py  ──>  imaging.py   (load, detect, clean)
                                           ──>  ocr.py       (tesseract subprocess)
                                           ──>  parsing.py   (text -> field guesses)
                  ──imports─>  mail.py      (.mbox -> images + body text)
     └── on save ─────────────────────────>     db.py        (sqlite)

config.py  — every path and tuning knob      resources.py — locate tesseract/tessdata
```

`pipeline` is the single seam between UI and processing, with two entry points
that return the same `PipelineOutput`:

- `process_image()` — a photo, whether added directly or pulled off an email.
- `process_email()` — an HTML e-receipt with **no** image. OCR is skipped
  entirely; the message body *is* the text. `PipelineOutput.scan` is None, so
  anything reading `.scan` must handle None (see `Processed.from_output` and
  `_render_image`, which shows an explanatory message instead of a canvas).

`as_record()` flattens into the exact column set `db.ReceiptStore` expects; the
UI merges the reviewed form values on top and saves. If you add a stored field,
touch `db.COLUMNS`, `db.SCHEMA`, and `PipelineOutput.as_record()` together — and
add it to `db.V2_COLUMNS`-style migration if existing databases must gain it.

`source_kind` distinguishes `image` / `email_image` / `email`. Duplicate
detection differs by kind: photos match on `source_sha256`, e-receipts on
`email_message_id`.

### Mailbox ingest (`mail.py`)

`.mbox` needs no third-party library — stdlib `mailbox` + `email` handle it.
BeautifulSoup is used only to flatten HTML bodies. Non-obvious rules:

- **Remote `<img src="https://…">` are never fetched.** They are logos and
  tracking pixels, and the app makes no network calls. Only real MIME image
  parts are extracted.
- **Image parts under `MIN_ATTACHMENT_BYTES` (8 KB) are ignored** as logos and
  spacers, otherwise every signature image becomes a "receipt".
- **`html_to_text` keeps each innermost `<tr>` on one line.** HTML receipts put
  a label and its amount in sibling cells; separating them breaks field
  extraction, which is line-oriented. Falls back to block text if the document
  yields fewer than 3 rows.
- A photo attached to an email takes the *receipt's own* printed date over the
  email `Date` header — the header is only a fallback.

### Config layering

`config.default.json` (shipped, bundled into the .app) is deep-merged under the
user's `config.json`, so partial user files work. `config.py::BUILTIN_DEFAULTS`
duplicates that JSON in code as a last-resort fallback — **edit both together or
defaults silently diverge.** Config location differs by mode: next to the source
in a checkout, `~/Library/Application Support/ReceiptScanner/` when frozen,
overridable via `RECEIPTSCANNER_CONFIG`.

Because the file is written as a full snapshot of defaults on first run, a stale
`config.json` pins old tuning values and masks improved defaults. Delete it when
pipeline defaults change.

### Frozen vs source resource resolution

`resources.py` is the only place that knows about PyInstaller layout. It probes
several roots (`sys._MEIPASS`, plus sibling `Resources`/`Frameworks`) and
several tesseract layouts, falling back to `PATH` and Homebrew locations. The
spec deliberately places the tesseract binary at the **root** of the collected
tree so it sits beside the dylibs PyInstaller rewrites to `@loader_path`. Moving
it into a subdirectory breaks the rpath. OCR passes `--tessdata-dir` explicitly
rather than trusting `TESSDATA_PREFIX`.

### UI threading

One worker thread processes the queue sequentially. It touches **no widgets** —
it communicates only by pushing tuples onto `self._events`, which the main
thread drains in `_poll_events` (an `after(80, ...)` loop). `ReceiptStore` is
main-thread only. Per-item form edits are stashed onto `QueueItem.form` by
`_stash_form()` whenever selection changes, so navigating away never loses
in-progress input.

## Pipeline decisions that look like bugs but are not

These were measured against `data/` by OCR keyword recall, not chosen by eye.
Naive "fixes" regress them badly — baseline recall was **0.00**, tuned is
**0.86** (mean confidence 19% → 64%, spurious words 830 → 160 per receipt).

- **Page detection runs on a ~700px downscale** (`detect_max_dim`). At full
  resolution the printed text carries more edge energy than the page border and
  the outline is never recovered.
- **Straightening uses `cv2.minAreaRect`, not a four-point perspective warp.**
  Corners recovered from an approximated contour are imprecise enough that the
  warp shears the text — measured recall 0.57 with the quad warp vs 0.83 with
  the rectangle. `four_point_transform` still exists and is used to apply the
  rectangle.
- **Candidates are ranked by paper-likeness, not area.** `_paper_contrast()`
  scores how much brighter a region is than the ring just outside it; ranking on
  area reliably picks the table the receipt is lying on.
- **CLAHE is off by default** (`clahe_clip: 0`). On flat paper it amplifies
  grain that the adaptive threshold then renders as speckle, which OCR reads as
  words.
- **The detected paper mask is warped alongside the image** and everything
  outside it is blanked to white, so table texture never reaches the threshold
  step.
- **Downscaling before thresholding is intentional** — averaging removes paper
  grain. `target_ocr_width` normalises in both directions when a page was found,
  but only ever enlarges when detection failed (the receipt is then a small part
  of a full frame and shrinking would destroy the text).

If you retune, do it against `data/` with a recall metric rather than by
inspecting one image.

### Field extraction (`parsing.py`)

Tuned for Indonesian receipts: `1.234.567,89` numbers, Indonesian month names,
day-first dates. Two subtleties:

- The total's keyword tier is decided by the **longest** matching keyword, not
  the first tier scanned — `"subtotal"` contains `"total"`, so first-match would
  rank a subtotal as the grand total.
- Amount candidates must be "money-shaped" (carry a group separator, or be a
  short digit run). Without this, card numbers, merchant IDs and approval codes
  win, since they are far larger than any real total.
- `guess_category` scores by **summed matched-keyword length**, not match count,
  so specificity wins: a Grab ride picked up outside a KFC stays Transportasi,
  while `grabfood` outweighs the bare `grab` it contains.

Every extracted value is a *suggestion*. The review step exists because OCR on a
creased receipt is sometimes wrong, so never silently auto-save an entry.
