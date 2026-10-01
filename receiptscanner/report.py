"""CSV export of saved entries over a date range, and the summary figures that
the CSV dialog and the attachment PDF both show.

Kept apart from the UI so the export can be exercised without a window, and
so the column layout lives in one place.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence

COLUMNS = ("Date", "Category", "Expense Detail", "Amount", "Currency")


@dataclass(frozen=True)
class ReportSummary:
    count: int
    # Sum across every currency. Only meaningful when `totals` has one entry;
    # rupiah and dollars cannot be added, so show `totals` instead.
    total: float
    start: str
    end: str
    totals: dict[str, float] = field(default_factory=dict)


def _currency_of(row: Mapping, default: str) -> str:
    """A row's currency, or `default` for legacy rows that never stored one."""
    try:
        return (row["currency"] or default) or ""
    except (KeyError, IndexError):
        return default


def summarize(
    rows: Sequence[Mapping], start: str, end: str, default_currency: str = ""
) -> ReportSummary:
    total = 0.0
    totals: dict[str, float] = {}
    for row in rows:
        try:
            amount = float(row["amount"] or 0)
        except (TypeError, ValueError):
            continue
        total += amount
        currency = _currency_of(row, default_currency)
        totals[currency] = totals.get(currency, 0.0) + amount
    return ReportSummary(
        count=len(rows), total=total, start=start, end=end, totals=totals
    )


def format_money(amount: float, currency: str = "") -> str:
    """`IDR 44,000` or `USD 56.48`: whole amounts drop the decimals."""
    text = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    return f"{currency} {text}".strip()


def format_totals(totals: dict[str, float]) -> str:
    """ "IDR 69,000 · USD 76.48": one figure per currency, never a mixed sum."""
    return " · ".join(
        format_money(amount, currency) for currency, amount in sorted(totals.items())
    )


def default_filename(start: str, end: str) -> str:
    return f"receipts_{start}_to_{end}.csv"


def write_csv(rows: Iterable[Mapping], path: Path, default_currency: str = "") -> int:
    """Write the report and return the number of data rows written.

    Encoded utf-8-sig so Excel picks up the BOM and renders Indonesian text
    correctly; amounts are written unformatted so a spreadsheet can sum them.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    header = list(COLUMNS)

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
                    _currency_of(row, default_currency),
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
