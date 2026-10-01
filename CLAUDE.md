# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Narmada (receipt tracker and expense report generator) is a local-only tkinter
desktop app that turns photos of receipts into expense entries: OpenCV preprocessing → Tesseract OCR (Indonesian model) → a human
review step → SQLite. There are no network calls anywhere in the pipeline, and
that is deliberate — keep it that way.

## Commands

```bash
# Every task is a make target; `make` lists them. pyproject.toml is the source of
# truth for metadata, dependency pins and pytest config; requirements.txt just
# installs it (`-e .[build,test]`).
make setup                                     # .venv + dependencies (Python 3.14 venv already at .venv)
brew install tesseract tesseract-lang          # needed for source runs only
make run                                       # launch the GUI

# Smoke-test the whole pipeline headlessly (pytest covers the logic; this covers the real thing)
.venv/bin/python main.py --self-test                     # engine/resource checks only
.venv/bin/python main.py --self-test data/IMG_5672.jpeg  # single image
.venv/bin/python main.py --self-test data/*.jpeg         # all four photo fixtures
.venv/bin/python main.py --self-test data/Unread.mbox    # every receipt in a mailbox

# Tests. The fast run needs no Tesseract, display or data/ and takes ~5 s
make test-fast                                              # -m "not ocr and not gui and not fixtures"
make test                                                   # everything this machine can run (~1 min)
.venv/bin/python -m pytest tests/test_report.py -k csv      # one file, one test

# Before changing extraction logic, snapshot the real receipts; afterwards diff them
.venv/bin/python tests/make_golden.py                       # writes data/golden.json (gitignored)
.venv/bin/python -m pytest -m fixtures

# Build and ship
make build      # dist/ReceiptScanner.app (embeds OpenCV, tesseract + dylibs, ind/eng models, the icon)
make verify     # self-contained check: Homebrew off the PATH, throwaway HOME and config
make package    # dist/ReceiptScanner-v<version>-macos-<arch>.zip
make clean      # build/, dist/, caches; keeps .venv, data/ and graphify-out/
make release    # guarded GitHub release of the current version; asks before publishing
```

`data/` holds the private fixtures: four real Indonesian receipt
photos and `Unread.mbox`, a Gmail export of five Grab e-receipts. `--self-test`
runs the full pipeline over whatever you pass and fails if anything yields zero
text. Point `RECEIPTSCANNER_CONFIG` at a scratch JSON file when running `--self-test`
by hand so it does not write into `~/Documents/ReceiptScanner`. The pytest suite
does this itself.

### Build and release

- **The version lives in one place:** `__version__` in `receiptscanner/__init__.py`.
  `pyproject.toml` reads it (`dynamic`, `attr`), and `ReceiptScanner.spec` reads
  it as text for the bundle's `CFBundleVersion` (the spec runs before the package
  is importable). Never type a version number anywhere else.
- **`make clean` deletes `dist/`, including any release zip you kept there.**
  `make build` does not clean it, so previous zips survive a rebuild; `make
  package` only overwrites the zip for the current version.
- **`scripts/release.sh` is outward-facing.** It runs only through `make release`
  and checks, in order: `gh` is logged in, branch is `master`, no uncommitted
  tracked changes, HEAD equals `origin/master`, the tag is free locally and on
  GitHub. Then it tests, builds, verifies, zips, and asks `[y/N]` before `gh
  release create` (`YES=1` skips the question, `DRAFT=1` makes a draft). Do not
  run it on the user's behalf without being asked to publish. Its guards were
  exercised against a bare repo and a fake `gh` that only logs its arguments;
  repeat that, never the real `gh`, if you change the script.
- `requirements.txt` is `-e .[build,test]`, so adding a dependency means editing
  `pyproject.toml` only. The project is not distributed as a wheel; the `.app` is
  the deliverable.

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
- **A destroyed window's Tk variables must not be finalised on a worker
  thread.** An autouse fixture in `conftest.py` runs `gc.collect()` on the main
  thread around every `gui` test. Without it a worker's collector pass calls
  `Variable.__del__`, which waits for a mainloop a pumped test never runs: the
  export looks hung, or raises "main thread is not in main loop".
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
                                           └──>  attachments.py (pdf of the receipt images)

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
before the render path existed. The *warning* shown while processing differs by
kind — photos match on `source_sha256`, e-receipts on `email_message_id`,
because a rendered image's hash is a property of the renderer rather than of the
receipt. What actually prevents a double entry is the dedupe key below.

`report.py` holds the CSV layout and date presets with no Tk import, so the
export can be tested headlessly; `ReportDialog` in `app.py` is only the window
around it. The CSV deliberately carries no total row — it would break sorting
and filtering — so the total is surfaced in the app instead. `format_money` and
`format_totals` live here too, because the CSV dialog and the attachment PDF show
the same figures; `app.py` aliases them as `_format_money` / `_format_totals`.

### Receipt attachments (`attachments.py`)

**Report → Generate Receipt Attachments (PDF)…** writes one PDF for a date range:
an index (with per-currency totals), then a captioned page per entry. It is a
second export beside the CSV and never touches it (`write_csv` and `COLUMNS` are
unchanged). No Tk import, like `report.py`. Non-obvious rules:

- **Where an entry's picture comes from, in order:** its stored scan; else, for
  a legacy `email` row that never got an image, the original mailbox
  (`source_path` ending `.mbox`), found by `email_message_id` and drawn with the
  same `render.render_email` a fresh import uses; else the original photo; else a
  marked placeholder page with the stored text. An `email_image` row's
  `source_path` is a picture, not a mailbox, so the suffix decides.
- **Strictly read-only.** Redrawing parses the mailbox into a temporary
  directory (`MailboxCache`, once per mailbox per export), runs no OCR, saves no
  PNG and writes no row. `expected_origin()` is the cheap estimate the dialog
  previews; it can disagree with `resolve_image()` for a scan that will not
  decode or a Message-ID the mailbox lacks.
- **Rows keep the order they are given** (`entries_between`: date, then id), so
  entry *n* in the PDF is CSV row *n*. Do not sort inside the module.
- **Pages are 1-bit A4 at 200 dpi** so Pillow writes CCITT G4: crisp text and a
  few MB for ~170 receipts, where JPEG pages blur text. Scans and renders are
  thresholded (`THRESHOLD`); a camera photo (`original`) is dithered instead. A
  receipt too tall to stay above `MIN_FIT_FRACTION` of its full-width size is
  sliced across continuation pages rather than shrunk.
- **All pages are built before anything is written**, then renamed into place
  from `<name>.part`: a cancel (`Cancelled`, checked before every entry) or a
  failure leaves no partial file and does not touch an earlier PDF. No author is
  written, so the PDF carries no personal metadata.
- **UI:** `AttachmentsDialog` subclasses `ReportDialog`, which exposes its
  wording, file type and write step as class-level hooks. The export runs on a
  worker thread that touches no widgets and reports through a queue; the window
  close button routes to cancel so a worker is never orphaned.

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

### Duplicates (`db.py`)

One row per **(title, amount, currency, date)**. `dedupe_key()` builds a JSON
key from the title (NFKC, casefolded, whitespace-collapsed), the amount as whole
cents (`Decimal` of the float's repr, half-up, so `0.1+0.2` equals `0.3`), the
upper-cased currency and the date. It returns `None` — opting out — when the
title, amount or date is missing, so untitled receipts are never merged. It is
stored in `receipts.dedupe_key` behind a UNIQUE index (SQLite permits many
NULLs).

- `save()` is an **upsert** returning `SaveResult(id, created)`. A match updates
  in place (same id, original `created_at`; empty new notes keep old ones). With
  `record_id` it edits that row; if the edit now matches a *different* row, the
  two merge onto the other and the edited row is deleted. A stale `record_id`
  inserts instead of silently updating nothing.
- `dedupe_key` is deliberately **not** in `COLUMNS`: it is derived inside
  `save()`, so a caller's record can never carry a stale key.
- **Migration v3** backs the database up first (sqlite backup API →
  `receipts.db.bak-YYYYMMDD-HHMMSS`, only if it has rows), then in one
  transaction adds the column, backfills, folds duplicates into the lowest id
  (the survivor keeps its own fields and provenance, borrowing notes only if it
  has none), and creates the UNIQUE index **last**. The index must not be in
  `SCHEMA`: `executescript(SCHEMA)` runs on every open and would fail against
  un-backfilled rows.
- **Transactions are explicit** (`isolation_level=None` + `_transaction()`),
  because `executescript()` silently commits an open transaction, which would
  break an all-or-nothing migration. `_apply_schema` therefore runs `SCHEMA`
  statement by statement. The explicit `ROLLBACK` matters on a live connection
  (a failed merge); on a failed *migration* the connection is closed, and
  closing rolls back by itself, so only `test_db_dedupe.py::TestAtomicity`
  actually proves it.
- Known limit: the title is the weak link. The same charge titled two ways is
  two entries (BCA notifications are titled with the sender address, forwards
  and merchant-line variants differ), and a void/reversal with the same title,
  amount and date merges into the original instead of cancelling it.

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

### Branding and the app icon

`assets/logo.png` is the brand logo (used by the README) and `assets/logo-app.png`
is the square icon master. The spec builds `icon.icns` from the master at build
time (`build_icns`: it adds the macOS artwork margin, 824 of 1024 px, and Pillow
writes every size), so no generated binary is committed. A full-bleed icon looks
oversized in the Dock, so do not feed the PNG to PyInstaller directly.

The product name is **Narmada**. `APP_TITLE` (window title, About box, Info.plist
`CFBundleName`) is the only user-visible name. `APP_NAME = "ReceiptScanner"`, the
`.app` filename, the bundle identifier and the `receiptscanner/` package name are
deliberately unchanged: `APP_NAME` is the on-disk config and data directory, and
changing it would orphan existing users' files.

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
- **Always search with graphify first when `graphify-out/graph.json` exists.** Before grepping, globbing or reading source to answer a question about the codebase, run `graphify query "<question>"`. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output. Fall back to grep/Read only after graphify has oriented you, or to inspect or edit specific lines.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- **Keep the graph current after significant changes — don't forget this step.** A significant change is a new or renamed module, function or class, a changed architecture or data flow, or edited docs (`README.md`, `CLAUDE.md`). Do it before you report the work as finished.
  - Code-only changes: run `graphify update .` (AST-only, no API cost).
  - Any change to docs (`README.md`, `CLAUDE.md`): run `/graphify . --update` instead. `graphify update .` does not re-read docs, so the graph would keep describing the old behaviour.
  - Check any node IDs a subagent proposes against `graphify-out/graph.json` before merging. They guess, and unresolved ones become dangling edges.
