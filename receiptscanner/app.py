"""Tkinter front end: queue, sequential pipeline run, side-by-side review."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageTk

from . import APP_TITLE, __version__
from .config import Config, ConfigError
from .db import ReceiptStore
from .imaging import SUPPORTED_EXTENSIONS, to_pil
from .mail import EmailReceipt, MailboxError, iter_mbox
from .ocr import OcrUnavailableError, describe_engine, get_engine
from .parsing import ParsedReceipt, parse_number
from .pipeline import PipelineOutput, process_email, process_image
from . import report

QUEUED = "Queued"
PROCESSING = "Processing"
NEEDS_REVIEW = "Needs review"
SAVED = "Saved"
FAILED = "Failed"

STATUS_COLOURS = {
    QUEUED: "#6b7280",
    PROCESSING: "#b45309",
    NEEDS_REVIEW: "#1d4ed8",
    SAVED: "#15803d",
    FAILED: "#b91c1c",
}

PREVIEW_MAX_SIDE = 1800
DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%d/%m/%y",
    "%d-%m-%y",
)


def parse_date_input(raw: str) -> str | None:
    """Accept the common written orders and return ISO, or None if unreadable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _format_money(amount: float, currency: str = "") -> str:
    text = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    return f"{currency} {text}".strip()


def _shrink(image: Image.Image, max_side: int = PREVIEW_MAX_SIDE) -> Image.Image:
    longest = max(image.size)
    if longest <= max_side:
        return image.copy()
    factor = max_side / longest
    size = (max(1, int(image.width * factor)), max(1, int(image.height * factor)))
    return image.resize(size, Image.Resampling.LANCZOS)


@dataclass
class Processed:
    """Pipeline result, reduced to what the UI needs to hold in memory.

    The previews are None for e-receipts parsed straight out of an email, which
    have no image at all.
    """

    record: dict[str, Any]
    ocr_text: str
    ocr_confidence: float
    ocr_words: int
    suggestion: ParsedReceipt
    stages: list[str]
    scanned_preview: Image.Image | None = None
    original_preview: Image.Image | None = None
    scanned_path: Path | None = None

    @classmethod
    def from_output(cls, output: PipelineOutput, config: Config) -> "Processed":
        scanned = original = None
        stages: list[str] = []
        if output.scan is not None:
            scanned = _shrink(to_pil(output.scan.scanned))
            original = _shrink(to_pil(output.scan.original_bgr), 1400)
            stages = list(output.scan.stages)
        elif output.email is not None:
            stages = ["parsed from email body (no image, OCR skipped)"]

        return cls(
            record=output.as_record(config),
            ocr_text=output.ocr.text,
            ocr_confidence=output.ocr.mean_confidence,
            ocr_words=output.ocr.word_count,
            suggestion=output.suggestion,
            stages=stages,
            scanned_preview=scanned,
            original_preview=original,
            scanned_path=output.scanned_path,
        )


@dataclass
class QueueItem:
    path: Path  # image file, or the mbox for text receipts
    status: str = QUEUED
    detail: str = ""
    processed: Processed | None = None
    error: str = ""
    db_id: int | None = None
    email: EmailReceipt | None = None
    label: str | None = None  # shown instead of the filename for emails
    # Live form state, kept per item so switching selection never loses edits.
    form: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.label or self.path.name

    @property
    def is_email_text(self) -> bool:
        """An e-receipt with no image: parsed from the body, never OCR'd."""
        return self.email is not None and not self.email.images


class ReportDialog(tk.Toplevel):
    """Pick a date range, see what it covers, write it out as CSV."""

    def __init__(
        self, parent: "ReceiptScannerApp", store: ReceiptStore, config: Config
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.config_data = config
        self.rows: list[Any] = []
        self._preview_job: str | None = None
        self._parent = parent  # held explicitly: used after this window is destroyed

        self.title("Generate Report")
        self.resizable(False, False)
        self.transient(parent)

        body = ttk.Frame(self, padding=(16, 14))
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            body, text="Export saved entries as CSV", font=("Helvetica", 13, "bold")
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W)
        ttk.Label(
            body,
            text="Columns: date, category, expense detail, amount.",
            foreground="#6b7280",
        ).grid(row=1, column=0, columnspan=4, sticky=tk.W, pady=(2, 12))

        default_start, default_end = report.presets()["This month"]
        self.var_start = tk.StringVar(value=default_start)
        self.var_end = tk.StringVar(value=default_end)

        ttk.Label(body, text="From").grid(row=2, column=0, sticky=tk.W)
        self.entry_start = ttk.Entry(body, textvariable=self.var_start, width=14)
        self.entry_start.grid(row=2, column=1, sticky=tk.W, padx=(6, 16))
        ttk.Label(body, text="To").grid(row=2, column=2, sticky=tk.W)
        self.entry_end = ttk.Entry(body, textvariable=self.var_end, width=14)
        self.entry_end.grid(row=2, column=3, sticky=tk.W, padx=(6, 0))

        ttk.Label(body, text="YYYY-MM-DD, inclusive", foreground="#9ca3af").grid(
            row=3, column=0, columnspan=4, sticky=tk.W, pady=(4, 10)
        )

        quick = ttk.Frame(body)
        quick.grid(row=4, column=0, columnspan=4, sticky=tk.W)
        for label, (start, end) in report.presets().items():
            ttk.Button(
                quick,
                text=label,
                width=11,
                command=lambda s=start, e=end: self._apply_range(s, e),
            ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(quick, text="All time", width=9, command=self._apply_all_time).pack(
            side=tk.LEFT
        )

        self.preview_label = ttk.Label(body, text="", font=("Helvetica", 12))
        self.preview_label.grid(
            row=5, column=0, columnspan=4, sticky=tk.W, pady=(14, 0)
        )

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=4, sticky=tk.EW, pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        self.export_button = ttk.Button(
            buttons, text="Export CSV…", command=self._export
        )
        self.export_button.pack(side=tk.RIGHT, padx=(0, 8))

        for widget in (self.entry_start, self.entry_end):
            widget.bind("<KeyRelease>", self._schedule_preview)
        self.bind("<Return>", lambda _event: self._export())
        self.bind("<Escape>", lambda _event: self.destroy())

        self._refresh_preview()
        self.entry_start.focus_set()

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 120
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()

    # -- range handling --------------------------------------------------
    def _apply_range(self, start: str, end: str) -> None:
        self.var_start.set(start)
        self.var_end.set(end)
        self._refresh_preview()

    def _apply_all_time(self) -> None:
        low, high = self.store.date_bounds()
        if not low or not high:
            self.preview_label.config(
                text="No saved entries yet.", foreground="#b45309"
            )
            return
        self._apply_range(low, high)

    def _schedule_preview(self, _event=None) -> None:
        if self._preview_job is not None:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(250, self._refresh_preview)

    def _current_range(self) -> tuple[str, str] | None:
        start = parse_date_input(self.var_start.get())
        end = parse_date_input(self.var_end.get())
        if start is None or end is None:
            return None
        return (start, end) if start <= end else (end, start)

    def _refresh_preview(self) -> None:
        self._preview_job = None
        span = self._current_range()
        if span is None:
            self.rows = []
            self.preview_label.config(
                text="Enter both dates as YYYY-MM-DD.", foreground="#b45309"
            )
            self.export_button.state(["disabled"])
            return

        start, end = span
        self.rows = self.store.entries_between(start, end)
        summary = report.summarize(self.rows, start, end)
        if summary.count == 0:
            self.preview_label.config(
                text=f"No entries between {start} and {end}.", foreground="#b45309"
            )
            self.export_button.state(["disabled"])
            return

        total = _format_money(summary.total, self.config_data.currency)
        self.preview_label.config(
            text=f"{summary.count} entr{'y' if summary.count == 1 else 'ies'} · {total}",
            foreground="#15803d",
        )
        self.export_button.state(["!disabled"])

    # -- writing ---------------------------------------------------------
    def _export(self) -> None:
        span = self._current_range()
        if span is None or not self.rows:
            return
        start, end = span

        # Release the modal grab so the native save panel can take focus.
        self.grab_release()
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Save report",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            initialfile=report.default_filename(start, end),
            initialdir=str(self.config_data.database_path.parent),
        )
        if not target:
            self.grab_set()
            return

        try:
            written = report.write_csv(
                self.rows, Path(target), self.config_data.currency
            )
        except OSError as exc:
            messagebox.showerror(
                "Could not write report", f"{target}\n\n{exc}", parent=self
            )
            self.grab_set()
            return

        summary = report.summarize(self.rows, start, end)
        total = _format_money(summary.total, self.config_data.currency)
        parent = self._parent
        self.destroy()
        parent.report_written(Path(target), written, total, start, end)


class ReceiptScannerApp(tk.Tk):
    def __init__(self, config: Config, store: ReceiptStore) -> None:
        super().__init__()
        self.config_data = config
        self.store = store

        self.items: list[QueueItem] = []
        self.current_index: int | None = None
        self._events: queue.Queue[tuple] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._photo: ImageTk.PhotoImage | None = None
        self._render_job: str | None = None
        self._zoom = 1.0
        self._loading_form = False

        self.title(f"{APP_TITLE} {__version__}")
        self.geometry("1480x940")
        self.minsize(1150, 720)

        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_events)
        self._refresh_counts()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(
            label="Add Images…", accelerator="Cmd+O", command=self.add_images
        )
        file_menu.add_command(
            label="Add from Mailbox…", accelerator="Cmd+M", command=self.add_mailbox
        )
        file_menu.add_command(
            label="Process Queue", accelerator="Cmd+R", command=self.start_processing
        )
        file_menu.add_separator()
        file_menu.add_command(label="Remove Selected", command=self.remove_selected)
        file_menu.add_command(label="Clear Queue", command=self.clear_queue)
        file_menu.add_separator()
        file_menu.add_command(
            label="Open Scans Folder",
            command=lambda: self._reveal(self.config_data.output_dir),
        )
        file_menu.add_command(
            label="Open Database Folder",
            command=lambda: self._reveal(self.config_data.database_path.parent),
        )
        file_menu.add_command(
            label="Open Config File",
            command=lambda: self._reveal(self.config_data.path),
        )
        menubar.add_cascade(label="File", menu=file_menu)

        report_menu = tk.Menu(menubar, tearoff=0)
        report_menu.add_command(
            label="Generate Report…", accelerator="Cmd+E", command=self.generate_report
        )
        menubar.add_cascade(label="Report", menu=report_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="OCR Engine Info…", command=self._show_engine_info)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)
        self.bind_all("<Command-o>", lambda _event: self.add_images())
        self.bind_all("<Command-m>", lambda _event: self.add_mailbox())
        self.bind_all("<Command-r>", lambda _event: self.start_processing())
        self.bind_all("<Command-s>", lambda _event: self.save_current())
        self.bind_all("<Command-e>", lambda _event: self.generate_report())

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill=tk.X)

        ttk.Button(bar, text="Add Images…", command=self.add_images).pack(side=tk.LEFT)
        ttk.Button(bar, text="Add Mailbox…", command=self.add_mailbox).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(bar, text="Remove", command=self.remove_selected).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(bar, text="Clear", command=self.clear_queue).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=12)

        self.process_button = ttk.Button(
            bar, text="▶  Process Queue", command=self.start_processing
        )
        self.process_button.pack(side=tk.LEFT)
        self.cancel_button = ttk.Button(
            bar, text="Stop", command=self.cancel_processing, state=tk.DISABLED
        )
        self.cancel_button.pack(side=tk.LEFT, padx=(6, 0))

        self.progress = ttk.Progressbar(bar, mode="determinate", length=240)
        self.progress.pack(side=tk.LEFT, padx=14)

        self.progress_label = ttk.Label(bar, text="")
        self.progress_label.pack(side=tk.LEFT)

    def _build_body(self) -> None:
        outer = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        outer.add(self._build_queue_pane(outer), weight=0)
        outer.add(self._build_image_pane(outer), weight=3)
        outer.add(self._build_review_pane(outer), weight=3)

    def _build_queue_pane(self, parent) -> ttk.Frame:
        frame = ttk.Labelframe(parent, text="Queue", padding=6)

        columns = ("status",)
        self.tree = ttk.Treeview(
            frame,
            columns=columns,
            show="tree headings",
            selectmode="extended",
            height=20,
        )
        self.tree.heading("#0", text="File")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=210, minwidth=140, stretch=True)
        self.tree.column("status", width=110, minwidth=90, stretch=False, anchor=tk.W)

        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        for status, colour in STATUS_COLOURS.items():
            self.tree.tag_configure(status, foreground=colour)

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        return frame

    def _build_image_pane(self, parent) -> ttk.Frame:
        frame = ttk.Labelframe(parent, text="Scanned image", padding=6)

        controls = ttk.Frame(frame)
        controls.pack(fill=tk.X, pady=(0, 6))

        self.view_mode = tk.StringVar(value="scanned")
        ttk.Radiobutton(
            controls,
            text="Scanned",
            value="scanned",
            variable=self.view_mode,
            command=self._render_image,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            controls,
            text="Original",
            value="original",
            variable=self.view_mode,
            command=self._render_image,
        ).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Separator(controls, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=10
        )

        self.fit_mode = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls, text="Fit", variable=self.fit_mode, command=self._render_image
        ).pack(side=tk.LEFT)
        ttk.Button(
            controls, text="−", width=3, command=lambda: self._nudge_zoom(1 / 1.25)
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            controls, text="+", width=3, command=lambda: self._nudge_zoom(1.25)
        ).pack(side=tk.LEFT, padx=(2, 0))
        self.zoom_label = ttk.Label(controls, text="—", width=6, anchor=tk.E)
        self.zoom_label.pack(side=tk.LEFT, padx=(6, 0))

        canvas_holder = ttk.Frame(frame)
        canvas_holder.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(
            canvas_holder, background="#3f3f46", highlightthickness=0
        )
        y_scroll = ttk.Scrollbar(
            canvas_holder, orient=tk.VERTICAL, command=self.canvas.yview
        )
        x_scroll = ttk.Scrollbar(
            canvas_holder, orient=tk.HORIZONTAL, command=self.canvas.xview
        )
        self.canvas.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        canvas_holder.rowconfigure(0, weight=1)
        canvas_holder.columnconfigure(0, weight=1)

        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(-1 * (e.delta // 3 or 1), "units"),
        )
        self.canvas.bind(
            "<Shift-MouseWheel>",
            lambda e: self.canvas.xview_scroll(-1 * (e.delta // 3 or 1), "units"),
        )

        self.stage_label = ttk.Label(frame, text="", foreground="#6b7280")
        self.stage_label.pack(fill=tk.X, pady=(6, 0))
        return frame

    def _build_review_pane(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent)

        self.review_banner = ttk.Label(
            frame,
            text="No image selected",
            anchor=tk.W,
            font=("Helvetica", 13, "bold"),
            padding=(8, 6),
        )
        self.review_banner.pack(fill=tk.X)

        splitter = ttk.PanedWindow(frame, orient=tk.VERTICAL)
        splitter.pack(fill=tk.BOTH, expand=True)

        splitter.add(self._build_ocr_box(splitter), weight=3)
        splitter.add(self._build_form_box(splitter), weight=2)
        return frame

    def _build_ocr_box(self, parent) -> ttk.Labelframe:
        box = ttk.Labelframe(parent, text="OCR result (editable)", padding=6)
        self.ocr_box = box

        self.ocr_text = ScrolledText(
            box,
            wrap=tk.WORD,
            font=("Menlo", 11),
            height=14,
            undo=True,
            relief=tk.FLAT,
            borderwidth=1,
        )
        self.ocr_text.pack(fill=tk.BOTH, expand=True)

        actions = ttk.Frame(box)
        actions.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(actions, text="Selected text →").pack(side=tk.LEFT)
        ttk.Button(
            actions, text="Name", width=7, command=lambda: self._selection_to("name")
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(
            actions,
            text="Amount",
            width=8,
            command=lambda: self._selection_to("amount"),
        ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(
            actions, text="Date", width=7, command=lambda: self._selection_to("date")
        ).pack(side=tk.LEFT, padx=(4, 0))

        self.ocr_meta = ttk.Label(actions, text="", foreground="#6b7280")
        self.ocr_meta.pack(side=tk.RIGHT)
        return box

    def _build_form_box(self, parent) -> ttk.Labelframe:
        box = ttk.Labelframe(parent, text="Expense entry", padding=(10, 8))

        self.var_date = tk.StringVar()
        self.var_category = tk.StringVar()
        self.var_name = tk.StringVar()
        self.var_amount = tk.StringVar()

        grid = ttk.Frame(box)
        grid.pack(fill=tk.BOTH, expand=True)
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Date").grid(row=0, column=0, sticky=tk.W, pady=4)
        date_row = ttk.Frame(grid)
        date_row.grid(row=0, column=1, sticky="ew", pady=4)
        self.entry_date = ttk.Entry(date_row, textvariable=self.var_date, width=16)
        self.entry_date.pack(side=tk.LEFT)
        ttk.Label(date_row, text="YYYY-MM-DD", foreground="#9ca3af").pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(
            date_row,
            text="Today",
            width=7,
            command=lambda: self.var_date.set(datetime.now().date().isoformat()),
        ).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(grid, text="Category").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.entry_category = ttk.Combobox(
            grid,
            textvariable=self.var_category,
            values=self.config_data.categories,
            state="normal",
        )
        self.entry_category.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(grid, text="Name").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.entry_name = ttk.Entry(grid, textvariable=self.var_name)
        self.entry_name.grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(grid, text="Amount").grid(row=3, column=0, sticky=tk.W, pady=4)
        amount_row = ttk.Frame(grid)
        amount_row.grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Label(
            amount_row, text=self.config_data.currency, foreground="#6b7280"
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.entry_amount = ttk.Entry(
            amount_row, textvariable=self.var_amount, width=20
        )
        self.entry_amount.pack(side=tk.LEFT)

        ttk.Label(grid, text="Notes").grid(row=4, column=0, sticky=tk.NW, pady=4)
        self.entry_notes = tk.Text(
            grid,
            height=3,
            wrap=tk.WORD,
            relief=tk.FLAT,
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#d4d4d8",
        )
        self.entry_notes.grid(row=4, column=1, sticky="ew", pady=4)

        buttons = ttk.Frame(box)
        buttons.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(buttons, text="◀ Previous", command=lambda: self._step(-1)).pack(
            side=tk.LEFT
        )
        ttk.Button(buttons, text="Skip ▶", command=lambda: self._step(1)).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(
            buttons, text="Reset to OCR", command=self._reset_to_suggestion
        ).pack(side=tk.LEFT, padx=(6, 0))

        self.save_button = ttk.Button(
            buttons, text="Save & Next  ⌘S", command=self.save_current
        )
        self.save_button.pack(side=tk.RIGHT)
        return box

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 4))
        bar.pack(fill=tk.X)
        self.status_label = ttk.Label(bar, text="Ready", anchor=tk.W)
        self.status_label.pack(side=tk.LEFT)
        self.counts_label = ttk.Label(bar, text="", anchor=tk.E, foreground="#6b7280")
        self.counts_label.pack(side=tk.RIGHT)

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------
    def add_images(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in SUPPORTED_EXTENSIONS)
        paths = filedialog.askopenfilenames(
            title="Select receipt photos",
            filetypes=[("Images", patterns), ("All files", "*.*")],
        )
        if not paths:
            return

        known = {item.path for item in self.items}
        added = skipped = 0
        for raw in paths:
            path = Path(raw)
            if path in known:
                skipped += 1
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                skipped += 1
                continue
            item = QueueItem(path=path)
            self.items.append(item)
            known.add(path)
            added += 1

        self._rebuild_tree()
        message = f"Added {added} image{'s' if added != 1 else ''}."
        if skipped:
            message += f" Skipped {skipped} (duplicate or unsupported)."
        self._set_status(message)
        if added and self.current_index is None:
            self._select_index(0)

    def add_mailbox(self) -> None:
        """Import a Gmail .mbox export.

        Photos attached to a message become ordinary image items and go through
        the full OCR pipeline. A message carrying no image is queued as a text
        e-receipt: its body is parsed directly, which is exact where OCR would
        only be a guess.
        """
        if self._is_busy():
            return
        path = filedialog.askopenfilename(
            title="Select a mailbox export",
            filetypes=[("Mailbox export", "*.mbox"), ("All files", "*.*")],
        )
        if not path:
            return

        mbox_path = Path(path)
        attachment_dir = self.config_data.output_dir.parent / "email_attachments"
        known = {item.path for item in self.items}
        added_images = added_text = skipped = 0

        self._set_status(f"Reading {mbox_path.name}…")
        self.update_idletasks()

        try:
            for receipt in iter_mbox(mbox_path, attachment_dir):
                if receipt.images:
                    for image in receipt.images:
                        if image in known:
                            skipped += 1
                            continue
                        self.items.append(
                            QueueItem(
                                path=image,
                                email=receipt,
                                label=f"{receipt.label[:40]} — {image.name}",
                            )
                        )
                        known.add(image)
                        added_images += 1
                elif receipt.body_text.strip():
                    self.items.append(
                        QueueItem(
                            path=mbox_path,
                            email=receipt,
                            label=f"✉ {receipt.label[:60]}",
                        )
                    )
                    added_text += 1
                else:
                    skipped += 1
        except MailboxError as exc:
            messagebox.showerror("Could not read mailbox", str(exc))
            return
        except Exception as exc:
            messagebox.showerror(
                "Could not read mailbox", f"{type(exc).__name__}: {exc}"
            )
            return

        self._rebuild_tree()
        parts = []
        if added_images:
            parts.append(f"{added_images} attached image(s)")
        if added_text:
            parts.append(f"{added_text} e-receipt(s) with no image")
        summary = (
            f"Imported {' and '.join(parts)} from {mbox_path.name}."
            if parts
            else f"No receipts found in {mbox_path.name}."
        )
        if skipped:
            summary += f" Skipped {skipped}."
        self._set_status(summary)

        if parts and self.current_index is None:
            self._select_index(0)

    def remove_selected(self) -> None:
        if self._is_busy():
            return
        indices = sorted(self._selected_indices(), reverse=True)
        if not indices:
            return
        for index in indices:
            del self.items[index]
        self.current_index = None
        self._rebuild_tree()
        self._clear_display()
        self._set_status("Removed from queue.")

    def clear_queue(self) -> None:
        if self._is_busy():
            return
        if self._unreviewed_count() and not messagebox.askyesno(
            "Clear queue", "Some receipts have not been saved yet. Clear anyway?"
        ):
            return
        self.items.clear()
        self.current_index = None
        self._rebuild_tree()
        self._clear_display()
        self._set_status("Queue cleared.")

    def _selected_indices(self) -> list[int]:
        return [int(iid) for iid in self.tree.selection() if iid.isdigit()]

    def _rebuild_tree(self) -> None:
        selection = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(self.items):
            self.tree.insert(
                "",
                tk.END,
                iid=str(index),
                text=f" {item.name}",
                values=(item.status,),
                tags=(item.status,),
            )
        for iid in selection:
            if iid in self.tree.get_children():
                self.tree.selection_add(iid)
        self._refresh_counts()

    def _update_row(self, index: int) -> None:
        if not (0 <= index < len(self.items)):
            return
        item = self.items[index]
        iid = str(index)
        if self.tree.exists(iid):
            label = item.status if not item.detail else f"{item.detail}"
            self.tree.item(iid, values=(label,), tags=(item.status,))
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        total = len(self.items)
        saved = sum(1 for item in self.items if item.status == SAVED)
        pending = self._unreviewed_count()
        failed = sum(1 for item in self.items if item.status == FAILED)

        parts = [f"{total} in queue", f"{saved} saved"]
        if pending:
            parts.append(f"{pending} to review")
        if failed:
            parts.append(f"{failed} failed")
        rows, amount = self.store.summary()
        parts.append(f"database: {rows} entries")
        self.counts_label.config(text="  ·  ".join(parts))

    def _unreviewed_count(self) -> int:
        return sum(1 for item in self.items if item.status == NEEDS_REVIEW)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    def _is_busy(self) -> bool:
        if self._worker is not None and self._worker.is_alive():
            self._set_status("Still processing — stop it first.")
            return True
        return False

    def start_processing(self) -> None:
        if self._is_busy():
            return
        pending = [
            index
            for index, item in enumerate(self.items)
            if item.status in (QUEUED, FAILED)
        ]
        if not pending:
            self._set_status(
                "Nothing to process. Add images first."
                if not self.items
                else "Every image has already been processed."
            )
            return

        # Text e-receipts never touch Tesseract, so only insist on it when the
        # batch actually contains an image to read.
        if any(not self.items[index].is_email_text for index in pending):
            try:
                get_engine()
            except OcrUnavailableError as exc:
                messagebox.showerror("Tesseract not available", str(exc))
                return

        self._cancel.clear()
        self.process_button.config(state=tk.DISABLED)
        self.cancel_button.config(state=tk.NORMAL)
        self.progress.config(maximum=len(pending), value=0)
        self._set_status(f"Processing {len(pending)} image(s) sequentially…")

        self._worker = threading.Thread(
            target=self._run_pipeline, args=(pending,), daemon=True
        )
        self._worker.start()

    def cancel_processing(self) -> None:
        self._cancel.set()
        self._set_status("Stopping after the current image…")

    def _run_pipeline(self, indices: list[int]) -> None:
        """Worker thread. Communicates only through self._events."""
        for position, index in enumerate(indices, start=1):
            if self._cancel.is_set():
                self._events.put(("cancelled", position - 1, len(indices)))
                break

            item = self.items[index]
            self._events.put(("begin", index, position, len(indices)))

            def report(message: str, _index: int = index) -> None:
                self._events.put(("stage", _index, message))

            try:
                if item.is_email_text:
                    output = process_email(
                        item.email, self.config_data, progress=report
                    )
                else:
                    output = process_image(
                        item.path, self.config_data, progress=report, email=item.email
                    )
                processed = Processed.from_output(output, self.config_data)
                self._events.put(("done", index, processed))
            except Exception as exc:  # keep one bad photo from killing the batch
                detail = f"{type(exc).__name__}: {exc}"
                self._events.put(("failed", index, detail, traceback.format_exc()))

        self._events.put(("finished",))

    def _poll_events(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(80, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]

        if kind == "begin":
            _, index, position, total = event
            item = self.items[index]
            item.status = PROCESSING
            item.detail = "Starting…"
            self._update_row(index)
            self.progress.config(value=position - 1)
            self.progress_label.config(text=f"{position} / {total}")
            self._set_status(f"[{position}/{total}] {item.name}")

        elif kind == "stage":
            _, index, message = event
            self.items[index].detail = message
            self._update_row(index)

        elif kind == "done":
            _, index, processed = event
            item = self.items[index]
            item.processed = processed
            item.status = NEEDS_REVIEW
            item.detail = ""
            item.error = ""
            item.form = self._initial_form(processed)
            self._update_row(index)
            self.progress.step(1)
            self._warn_if_duplicate(item, processed)
            if (
                self.current_index is None
                or self.items[self.current_index].status != NEEDS_REVIEW
            ):
                self._select_index(index)
            elif self.current_index == index:
                self._show_item(index)

        elif kind == "failed":
            _, index, detail, trace = event
            item = self.items[index]
            item.status = FAILED
            item.detail = "Failed"
            item.error = detail
            self._update_row(index)
            self.progress.step(1)
            self._set_status(f"{item.name}: {detail}")
            sys.stderr.write(trace)

        elif kind == "cancelled":
            _, completed, total = event
            self._set_status(f"Stopped after {completed} of {total} images.")

        elif kind == "finished":
            self._on_processing_finished()

    def _on_processing_finished(self) -> None:
        self._worker = None
        self.process_button.config(state=tk.NORMAL)
        self.cancel_button.config(state=tk.DISABLED)
        self.progress_label.config(text="")
        self.progress.config(value=0)

        pending = self._unreviewed_count()
        failed = sum(1 for item in self.items if item.status == FAILED)
        self._refresh_counts()

        if pending:
            first = next(
                index
                for index, item in enumerate(self.items)
                if item.status == NEEDS_REVIEW
            )
            self._select_index(first)
            self._set_status(f"{pending} receipt(s) ready for review.")
            messagebox.showinfo(
                "Review required",
                f"{pending} receipt(s) processed and waiting for review."
                + (f"\n{failed} image(s) failed." if failed else "")
                + "\n\nCheck each scan against the OCR text, fill in the entry "
                "fields, then press “Save & Next”.",
            )
        elif failed:
            self._set_status(f"Finished — {failed} image(s) failed.")
        else:
            self._set_status("Finished.")

    def _warn_if_duplicate(self, item: QueueItem, processed: Processed) -> None:
        existing = None
        if item.is_email_text:
            existing = self.store.find_by_message_id(
                processed.record.get("email_message_id") or ""
            )
        else:
            existing = self.store.find_by_hash(
                processed.record.get("source_sha256") or ""
            )

        if existing is not None:
            item.detail = "Possible duplicate"
            self._set_status(
                f"{item.name} matches an entry already in the database (id {existing['id']})."
            )

    # ------------------------------------------------------------------
    # Review form
    # ------------------------------------------------------------------
    def _initial_form(self, processed: Processed) -> dict[str, str]:
        suggestion = processed.suggestion
        return {
            "entry_date": suggestion.entry_date,
            "category": suggestion.category,
            "name": suggestion.name,
            "amount": suggestion.amount,
            "notes": "",
            "ocr_text": processed.ocr_text,
        }

    def _on_tree_select(self, _event=None) -> None:
        indices = self._selected_indices()
        if len(indices) != 1:
            return
        index = indices[0]
        if index == self.current_index:
            return
        self._stash_form()
        self.current_index = index
        self._show_item(index)

    def _select_index(self, index: int) -> None:
        if not (0 <= index < len(self.items)):
            return
        self._stash_form()
        self.current_index = index
        iid = str(index)
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        self._show_item(index)

    def _stash_form(self) -> None:
        """Persist in-progress edits back onto the item being navigated away from."""
        if self.current_index is None or self._loading_form:
            return
        if not (0 <= self.current_index < len(self.items)):
            return
        item = self.items[self.current_index]
        if item.processed is None:
            return
        item.form = {
            "entry_date": self.var_date.get(),
            "category": self.var_category.get(),
            "name": self.var_name.get(),
            "amount": self.var_amount.get(),
            "notes": self.entry_notes.get("1.0", tk.END).strip(),
            "ocr_text": self.ocr_text.get("1.0", tk.END).strip(),
        }

    def _show_item(self, index: int) -> None:
        item = self.items[index]
        self._loading_form = True
        try:
            if item.processed is None:
                self._clear_display(keep_selection=True)
                if item.status == FAILED:
                    self.review_banner.config(
                        text=f"{item.name} — failed", foreground="#b91c1c"
                    )
                    self.ocr_text.delete("1.0", tk.END)
                    self.ocr_text.insert("1.0", item.error or "Processing failed.")
                else:
                    self.review_banner.config(
                        text=f"{item.name} — not processed yet", foreground="#6b7280"
                    )
                return

            processed = item.processed
            position = index + 1
            reviewed = sum(1 for entry in self.items if entry.status == SAVED)
            state = "saved" if item.status == SAVED else "awaiting review"
            self.review_banner.config(
                text=f"Review {position} of {len(self.items)} — {item.name}  ({state}, {reviewed} saved)",
                foreground=STATUS_COLOURS.get(item.status, "#111827"),
            )

            form = item.form or self._initial_form(processed)
            self.var_date.set(form.get("entry_date", ""))
            self.var_category.set(form.get("category", ""))
            self.var_name.set(form.get("name", ""))
            self.var_amount.set(form.get("amount", ""))

            self.entry_notes.delete("1.0", tk.END)
            self.entry_notes.insert("1.0", form.get("notes", ""))

            self.ocr_text.delete("1.0", tk.END)
            self.ocr_text.insert("1.0", form.get("ocr_text", processed.ocr_text))

            if item.is_email_text:
                self.ocr_box.config(text="Email receipt text (editable)")
                sender = processed.record.get("email_from") or "unknown sender"
                self.ocr_meta.config(
                    text=f"{processed.ocr_words} words · from {sender}"
                )
            else:
                self.ocr_box.config(text="OCR result (editable)")
                self.ocr_meta.config(
                    text=f"{processed.ocr_words} words · mean confidence {processed.ocr_confidence:.0f}% · "
                    f"lang {processed.record.get('ocr_lang', '?')}"
                )
            self.stage_label.config(text="Pipeline: " + " → ".join(processed.stages))
            self.save_button.config(
                text="Update entry" if item.status == SAVED else "Save & Next  ⌘S"
            )
        finally:
            self._loading_form = False

        self._render_image()

    def _clear_display(self, keep_selection: bool = False) -> None:
        if not keep_selection:
            self.review_banner.config(text="No image selected", foreground="#6b7280")
        self.canvas.delete("all")
        self._photo = None
        self.ocr_text.delete("1.0", tk.END)
        self.ocr_meta.config(text="")
        self.stage_label.config(text="")
        self.entry_notes.delete("1.0", tk.END)
        for var in (self.var_date, self.var_category, self.var_name, self.var_amount):
            var.set("")

    def _reset_to_suggestion(self) -> None:
        if self.current_index is None:
            return
        item = self.items[self.current_index]
        if item.processed is None:
            return
        item.form = self._initial_form(item.processed)
        self._show_item(self.current_index)
        self._set_status("Fields reset to the OCR suggestion.")

    def _selection_to(self, field_name: str) -> None:
        try:
            selection = self.ocr_text.get(tk.SEL_FIRST, tk.SEL_LAST).strip()
        except tk.TclError:
            self._set_status("Select some text in the OCR panel first.")
            return
        if not selection:
            return

        if field_name == "name":
            self.var_name.set(" ".join(selection.split()))
        elif field_name == "amount":
            value = parse_number(selection)
            if value is None:
                self._set_status(f"Could not read an amount from “{selection}”.")
                return
            self.var_amount.set(
                str(int(value))
                if value == value.to_integral_value()
                else f"{value:.2f}"
            )
        elif field_name == "date":
            from .parsing import extract_date

            parsed = extract_date(selection)
            if not parsed:
                self._set_status(f"Could not read a date from “{selection}”.")
                return
            self.var_date.set(parsed)

    def _step(self, direction: int) -> None:
        if not self.items:
            return
        start = self.current_index if self.current_index is not None else -1
        target = max(0, min(len(self.items) - 1, start + direction))
        if target != start:
            self._select_index(target)

    def _next_needing_review(self, after: int) -> int | None:
        order = list(range(after + 1, len(self.items))) + list(range(0, after + 1))
        for index in order:
            if self.items[index].status == NEEDS_REVIEW:
                return index
        return None

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------
    def _validated_entry(self) -> dict[str, Any] | None:
        raw_date = self.var_date.get().strip()
        if not raw_date:
            messagebox.showwarning(
                "Date required", "Enter the receipt date as YYYY-MM-DD."
            )
            self.entry_date.focus_set()
            return None

        entry_date = parse_date_input(raw_date)
        if entry_date is None:
            messagebox.showwarning(
                "Invalid date",
                f"“{raw_date}” is not a date I can read.\nUse YYYY-MM-DD.",
            )
            self.entry_date.focus_set()
            return None

        raw_amount = self.var_amount.get().strip()
        if not raw_amount:
            messagebox.showwarning("Amount required", "Enter the receipt total.")
            self.entry_amount.focus_set()
            return None
        amount = parse_number(raw_amount)
        if amount is None or amount < 0:
            messagebox.showwarning(
                "Invalid amount", f"“{raw_amount}” is not a valid amount."
            )
            self.entry_amount.focus_set()
            return None

        name = self.var_name.get().strip()
        if not name and not messagebox.askyesno(
            "No name", "Save this entry without a name?"
        ):
            self.entry_name.focus_set()
            return None

        return {
            "entry_date": entry_date,
            "category": self.var_category.get().strip() or "Lainnya",
            "name": name,
            "amount": float(amount),
            "notes": self.entry_notes.get("1.0", tk.END).strip() or None,
            "ocr_text": self.ocr_text.get("1.0", tk.END).strip(),
        }

    def save_current(self) -> None:
        if self.current_index is None:
            self._set_status("Select a processed receipt first.")
            return

        index = self.current_index
        item = self.items[index]
        if item.processed is None:
            self._set_status("This image has not been processed yet.")
            return

        entry = self._validated_entry()
        if entry is None:
            return

        record = dict(item.processed.record)
        record.update(entry)

        try:
            item.db_id = self.store.save(record, record_id=item.db_id)
        except Exception as exc:
            messagebox.showerror(
                "Database error", f"Could not save the entry:\n\n{exc}"
            )
            return

        item.status = SAVED
        item.detail = ""
        item.form = {
            **entry,
            "amount": self.var_amount.get().strip(),
            "notes": entry["notes"] or "",
            "ocr_text": entry["ocr_text"],
        }
        self._update_row(index)
        self._set_status(
            f"Saved “{entry['name'] or item.name}” — {self.config_data.currency} {entry['amount']:,.2f} (id {item.db_id})."
        )

        following = self._next_needing_review(index)
        if following is not None:
            self._select_index(following)
        else:
            self._show_item(index)
            if self.items and all(i.status in (SAVED, FAILED) for i in self.items):
                failed = sum(1 for i in self.items if i.status == FAILED)
                messagebox.showinfo(
                    "All done",
                    f"Every processed receipt has been reviewed and saved."
                    + (
                        f"\n\n{failed} image(s) failed and were not saved."
                        if failed
                        else ""
                    ),
                )

    # ------------------------------------------------------------------
    # Image rendering
    # ------------------------------------------------------------------
    def _current_preview(self) -> Image.Image | None:
        if self.current_index is None:
            return None
        item = self.items[self.current_index]
        if item.processed is None:
            return None
        if self.view_mode.get() == "original":
            return item.processed.original_preview
        return item.processed.scanned_preview

    def _on_canvas_resize(self, _event=None) -> None:
        if not self.fit_mode.get():
            return
        if self._render_job is not None:
            self.after_cancel(self._render_job)
        self._render_job = self.after(120, self._render_image)

    def _nudge_zoom(self, factor: float) -> None:
        self.fit_mode.set(False)
        self._zoom = max(0.1, min(6.0, self._zoom * factor))
        self._render_image()

    def _canvas_message(self, *lines: str) -> None:
        self._photo = None
        self.zoom_label.config(text="—")
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        self.canvas.create_text(
            width / 2,
            height / 2,
            text="\n".join(lines),
            fill="#d4d4d8",
            justify=tk.CENTER,
            font=("Helvetica", 13),
            width=max(width - 60, 120),
        )

    def _render_image(self) -> None:
        self._render_job = None
        image = self._current_preview()
        self.canvas.delete("all")
        if image is None:
            item = (
                self.items[self.current_index]
                if self.current_index is not None
                else None
            )
            if item is not None and item.is_email_text and item.processed is not None:
                self._canvas_message(
                    "✉  E-receipt with no image",
                    "",
                    "The figures were read straight from the message body,",
                    "so there was nothing to scan and OCR was skipped.",
                )
            else:
                self._photo = None
                self.zoom_label.config(text="—")
            return

        canvas_width = max(self.canvas.winfo_width(), 1)
        canvas_height = max(self.canvas.winfo_height(), 1)

        if self.fit_mode.get():
            scale = min(canvas_width / image.width, canvas_height / image.height)
            scale = min(scale, 1.0) if scale > 0 else 1.0
            self._zoom = scale
        else:
            scale = self._zoom

        width = max(1, int(image.width * scale))
        height = max(1, int(image.height * scale))
        resample = Image.Resampling.LANCZOS if scale < 1 else Image.Resampling.NEAREST
        self._photo = ImageTk.PhotoImage(image.resize((width, height), resample))

        x = max(width, canvas_width) / 2
        y = max(height, canvas_height) / 2
        self.canvas.create_image(x, y, image=self._photo, anchor=tk.CENTER)
        self.canvas.config(
            scrollregion=(0, 0, max(width, canvas_width), max(height, canvas_height))
        )
        self.zoom_label.config(text=f"{scale * 100:.0f}%")

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _set_status(self, message: str) -> None:
        self.status_label.config(text=message)

    def _reveal(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if sys.platform == "darwin":
                command = ["open"] + (["-R"] if path.is_file() else []) + [str(path)]
                subprocess.run(command, check=False, capture_output=True)
            elif sys.platform.startswith("win"):
                subprocess.run(["explorer", str(path)], check=False)
            else:
                subprocess.run(
                    ["xdg-open", str(path.parent if path.is_file() else path)],
                    check=False,
                )
        except Exception as exc:
            messagebox.showerror("Could not open", f"{path}\n\n{exc}")

    def generate_report(self) -> None:
        """Export saved entries over a date range as CSV."""
        rows, _total = self.store.summary()
        if not rows:
            messagebox.showinfo(
                "Nothing to report",
                "No entries have been saved yet.\n\n"
                "Process some receipts and save them first.",
            )
            return
        dialog = ReportDialog(self, self.store, self.config_data)
        self.wait_window(dialog)

    def report_written(
        self, path: Path, count: int, total: str, start: str, end: str
    ) -> None:
        """Called by ReportDialog once the file is on disk."""
        self._set_status(f"Report saved: {path.name} — {count} entries, {total}.")
        if messagebox.askyesno(
            "Report saved",
            f"{count} entr{'y' if count == 1 else 'ies'} from {start} to {end}\n"
            f"Total: {total}\n\n{path}\n\nShow it in Finder?",
        ):
            self._reveal(path)

    def _show_engine_info(self) -> None:
        messagebox.showinfo("OCR engine", describe_engine())

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About",
            f"{APP_TITLE} {__version__}\n\n"
            "Local receipt scanning: OpenCV preprocessing, Tesseract OCR, SQLite storage.\n"
            "Nothing leaves this machine.\n\n"
            f"Config:   {self.config_data.path}\n"
            f"Scans:    {self.config_data.output_dir}\n"
            f"Database: {self.config_data.database_path}",
        )

    def _on_close(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            if not messagebox.askyesno(
                "Quit", "Processing is still running. Quit anyway?"
            ):
                return
            self._cancel.set()

        pending = self._unreviewed_count()
        if pending and not messagebox.askyesno(
            "Unreviewed receipts",
            f"{pending} receipt(s) have not been saved to the database yet.\n\nQuit anyway?",
        ):
            return

        self.store.close()
        self.destroy()


def run() -> int:
    try:
        config = Config.load()
        config.ensure_directories()
    except (ConfigError, OSError) as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Configuration error", str(exc))
        root.destroy()
        return 1

    try:
        store = ReceiptStore(config.database_path)
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Database error", f"Could not open {config.database_path}:\n\n{exc}"
        )
        root.destroy()
        return 1

    app = ReceiptScannerApp(config, store)

    try:
        get_engine()
    except OcrUnavailableError as exc:
        app.after(300, lambda: messagebox.showwarning("Tesseract not found", str(exc)))

    app.mainloop()
    return 0
