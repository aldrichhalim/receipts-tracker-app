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

# Smoke-test the whole pipeline headlessly (pytest covers the logic; this covers the real thing)
.venv/bin/python main.py --self-test                     # engine/resource checks only
.venv/bin/python main.py --self-test data/IMG_5672.jpeg  # single image
.venv/bin/python main.py --self-test data/*.jpeg         # all four photo fixtures
.venv/bin/python main.py --self-test data/Unread.mbox    # every receipt in a mailbox

# Tests. The fast run needs no Tesseract, display or data/ and takes ~2 s
.venv/bin/python -m pytest -m "not ocr and not gui and not fixtures"
.venv/bin/python -m pytest                                  # everything this machine can run (~1 min)
.venv/bin/python -m pytest tests/test_report.py -k csv      # one file, one test

# Before changing extraction logic, snapshot the real receipts; afterwards diff them
.venv/bin/python tests/make_golden.py                       # writes data/golden.json (gitignored)
.venv/bin/python -m pytest -m fixtures

# Build the .app (embeds OpenCV, the tesseract binary + dylibs, ind/eng models)
.venv/bin/python -m PyInstaller --noconfirm --clean ReceiptScanner.spec

# Verify a build is genuinely self-contained (Homebrew off the PATH)
env -u TESSDATA_PREFIX PATH=/usr/bin:/bin \
  ./dist/ReceiptScanner.app/Contents/MacOS/ReceiptScanner --self-test data/IMG_5672.jpeg
```

`data/` holds the private fixtures: four real Indonesian receipt
photos and `Unread.mbox`, a Gmail export of five Grab e-receipts. `--self-test`
runs the full pipeline over whatever you pass and fails if anything yields zero
text. Point `RECEIPTSCANNER_CONFIG` at a scratch JSON file when running `--self-test`
by hand so it does not write into `~/Documents/ReceiptScanner`. The pytest suite
does this itself.

### Tests

`tests/` has no dependency on `data/`, which is gitignored because it holds real
names, card digits and tax IDs: unit tests use small synthetic OCR-like text and
synthetic mailboxes (`tests/helpers.py`). Rules that are easy to break:

- **Isolation is automatic and must stay so.** An autouse fixture in
  `conftest.py` points `HOME` and `RECEIPTSCANNER_CONFIG` at `tmp_path`. An
  earlier unisolated script wrote a row and several scans into the real
  database. `test_isolation.py` proves it holds; never build a `Config` or
  `ReceiptStore` against a real path in a test.
- **Markers skip themselves:** `ocr` (needs Tesseract), `gui` (needs a display),
  `fixtures` (needs `data/golden.json`). Do not add a marker without an
  auto-skip in `conftest.py`.
- **The `gui` probe runs in a subprocess.** A second `tkinter.Tk()` in the
  pytest process segfaults on macOS, so the process must not create a throwaway
  root just to check for a display.
- **Characterise before you change, then red → green.** The suite pins the
  tuned IDR behaviour so refactors cannot trade it away. When adding behaviour,
  write the failing test first; if an existing test has to change, treat it as a
  regression until proven otherwise. After writing tests, sanity-check them by
  injecting a bug into the code under test: a suite that has never failed has
  not proven it can.
- **Known defects are strict `xfail`s, not frozen as correct:** the
  unlabelled-override heuristic in `extract_amount` (a discounted item price can
  replace the total) and CSV formula injection in `write_csv`. Fixing one turns
  its test into an unexpected pass, which forces the marker off.
- `data/golden.json` is a local regression net for the real receipts, not
  a spec: an intended improvement legitimately changes it. Review each diff,
  then regenerate.

The GUI can also be driven headlessly for integration testing: construct
`ReceiptScannerApp`, call `withdraw()`, stub `app.messagebox`/`app.filedialog`,
then pump `app.update()` in a loop while the worker thread runs.

## Architecture

Data flows in one direction through `receiptscanner/`:

```
app.py (tkinter)  ──calls──>  pipeline.py  ──>  imaging.py   (load, detect, clean)
                                           ──>  ocr.py       (tesseract subprocess)
                                           ──>  parsing.py   (text -> field guesses)
                                           ──>  render.py    (email markup -> image)
                  ──imports─>  mail.py      (.mbox -> images + rows + body text)
     └── on save ─────────────────────────>     db.py        (sqlite)
     └── Report menu ─────────────────────>     report.py    (csv over a date range)

config.py  — every path and tuning knob      resources.py — locate tesseract/tessdata
```

`pipeline` is the single seam between UI and processing, with two entry points
that return the same `PipelineOutput`:

- `process_image()` — a photo, whether added directly or pulled off an email.
- `process_email()` — an HTML e-receipt with no photo of its own. `render.py`
  draws the markup as an image, and that image goes through OCR like any other
  receipt, so the review step always has something to look at.

`process_email` deliberately **skips `imaging.scan()`**: a render is already
clean black on white, so page detection has no page to find, and the shadow /
unsharp / adaptive-threshold chain could only damage glyphs that need no
repair. It builds a `ScanResult` by hand instead, and re-reads the PNG it just
wrote purely to obtain a real `SourceInfo`. It also overrides the OCR page
segmentation with `email_render.psm` (4, "single column of variable-size text")
— the `psm 6` tuned for photographed receipts reads a wide label/amount gutter
worse.

`PipelineOutput.scan` is still typed `| None`, so anything reading it must
handle None, but no current path produces that.

`as_record()` flattens into the exact column set `db.ReceiptStore` expects; the
UI merges the reviewed form values on top and saves. If you add a stored field,
touch `db.COLUMNS`, `db.SCHEMA`, and `PipelineOutput.as_record()` together — and
add it to `db.V2_COLUMNS`-style migration if existing databases must gain it.

`source_kind` distinguishes `image` / `email_image` (photo attached to a
message) / `email_render` (drawn from markup). `email` is legacy: rows written
before the render path existed. Duplicate detection differs by kind — photos
match on `source_sha256`, e-receipts on `email_message_id`, because a rendered
image's hash is a property of the renderer rather than of the receipt.

`report.py` holds the CSV layout and date presets with no Tk import, so the
export can be tested headlessly; `ReportDialog` in `app.py` is only the window
around it. The CSV deliberately carries no total row — it would break sorting
and filtering — so the total is surfaced in the app instead.

### Currency (`parsing.py`)

`detect_currency` counts `Rp`/`IDR`/`rupiah` against `$`/`US$`/`USD` (with
look-arounds so `S$`, `BUSD`, `USDA` do not count); a tie returns the caller's
default (`config.currency`). It is detected **first** and changes two rules:

- **Amount floor.** IDR drops values under 100 (they are quantities). USD cannot
  (`$0.99` is a total), so a USD value must carry cents *or* sit on a line that
  names the currency, which still rejects a bare `Total 3`.
- **Numeric date order.** Day-first for IDR, month-first for USD, each falling
  back to the other when impossible (`25/12/2026`). Month-name dates
  (`Aug 17, 2026`) are unambiguous and parse for both.

`parse_number` needed no change: its "1-2 digits after the last separator is a
decimal" rule already reads `10.50`, `1,234.56` and BCA's `USD 56,48`.
`ParsedReceipt.currency` is `""` for empty text, and `as_record` falls back to
`config.currency`. Reports total **per currency** (`ReportSummary.totals`);
`.total` is the cross-currency sum and is only meaningful for a single one. The
CSV has a `Currency` column.

### Mailbox ingest (`mail.py`)

`.mbox` needs no third-party library — stdlib `mailbox` + `email` handle it.
BeautifulSoup is used only to flatten HTML bodies. Non-obvious rules:

- **Remote `<img src="https://…">` are never fetched.** They are logos and
  tracking pixels, and the app makes no network calls. Only real MIME image
  parts are extracted.
- **Image parts under `MIN_ATTACHMENT_BYTES` (8 KB) are ignored** as logos and
  spacers, otherwise every signature image becomes a "receipt".
- **`structured_rows` keeps each innermost `<tr>` intact**, as a list of cells.
  HTML receipts put a label and its amount in sibling cells; separating them
  breaks field extraction, which is line-oriented, and costs the renderer the
  structure it needs to put the pair on one visual line. Falls back to block
  text if the document yields fewer than 3 rows. `html_to_text` is now just
  `structured_rows` joined, so the two cannot drift apart.
- **`body_html` is kept alongside `body_text`.** The plain-text alternative used
  to win outright, which threw away the markup the renderer needs — most
  e-receipts are `multipart/alternative` and carry both.
- A photo attached to an email takes the *receipt's own* printed date over the
  email `Date` header — the header is only a fallback.

### Rendering e-receipts (`render.py`)

Pillow only — no browser engine, so no possibility of a network call, and
nothing new to bundle. `mail.structured_rows` supplies rows; the renderer
measures them, sizes a canvas to the content, then draws.

- **A 2-cell row is drawn label-left / value-right on one baseline.** This is
  the case worth rendering: it reproduces receipt structure that a line-oriented
  reader would have to infer.
- **Characters the font cannot draw are stripped** (`_UNDRAWABLE`: emoji,
  dingbats, arrows, private-use icon fonts). Pillow draws them as tofu boxes,
  which OCR reads as invented words.
- **No borders or rules are drawn.** Horizontal lines make Tesseract emit
  spurious `—`/`_` words — the same reason `imaging.py` despeckles.
- Measured on `data/Unread.mbox`, the render path returns OCR at ~93% mean
  confidence and extracts date, category, merchant and amount identically to
  the old text path on all five receipts.

Be clear-eyed about the trade: the renderer draws text the app already holds, so
OCR is re-reading our own drawing and cannot beat parsing that text directly.
What it buys is a picture to review against, and the 2-cell layout rule. Judge
changes here against `--self-test data/Unread.mbox`, not by eye.

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

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
