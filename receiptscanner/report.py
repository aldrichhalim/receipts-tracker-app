"""CSV export of saved entries over a date range.

Kept apart from the UI so the export can be exercised without a window, and
so the column layout lives in one place.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence

COLUMNS = ("Date", "Category", "Expense Detail", "Amount")


@dataclass(frozen=True)
class ReportSummary:
    count: int
    total: float
    start: str
    end: str


def summarize(rows: Sequence[Mapping], start: str, end: str) -> ReportSummary:
    total = 0.0
    for row in rows:
        try:
            total += float(row["amount"] or 0)
        except (TypeError, ValueError):
            continue
    return ReportSummary(count=len(rows), total=total, start=start, end=end)


def default_filename(start: str, end: str) -> str:
    return f"receipts_{start}_to_{end}.csv"


def write_csv(rows: Iterable[Mapping], path: Path, currency: str = "") -> int:
    """Write the report and return the number of data rows written.

    Encoded utf-8-sig so Excel picks up the BOM and renders Indonesian text
    correctly; amounts are written unformatted so a spreadsheet can sum them.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    header = list(COLUMNS)
    if currency:
        header[-1] = f"Amount ({currency})"

    written = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            try:
                amount = f"{float(row['amount'] or 0):.2f}"
            except (TypeError, ValueError):
                amount = ""
            writer.writerow(
                [
                    row["entry_date"] or "",
                    row["category"] or "",
                    row["name"] or "",
                    amount,
                ]
            )
            written += 1
    return written


# -- range presets ------------------------------------------------------


def month_start(today: date) -> date:
    return today.replace(day=1)


def presets(today: date | None = None) -> dict[str, tuple[str, str]]:
    """Named date ranges for the export dialog, as ISO string pairs."""
    today = today or date.today()
    this_month = month_start(today)
    last_month_end = this_month - timedelta(days=1)

    return {
        "This month": (this_month.isoformat(), today.isoformat()),
        "Last month": (
            month_start(last_month_end).isoformat(),
            last_month_end.isoformat(),
        ),
        "This year": (today.replace(month=1, day=1).isoformat(), today.isoformat()),
    }
