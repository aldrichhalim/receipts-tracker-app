"""extract_date: day-first Indonesian dates, with a US-order tolerance."""

import pytest

from receiptscanner.parsing import extract_date


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-07-14", "2026-07-14"),
        ("2026/7/4", "2026-07-04"),
        ("Tgl 2026.07.14", "2026-07-14"),
    ],
)
def test_iso_dates(text, expected):
    assert extract_date(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("12 Agustus 2024", "2024-08-12"),
        ("12-Agu-24", "2024-08-12"),
        ("03 JUN,26", "2026-06-03"),  # OCR often reads "." as ","
        ("5 Okt 2025", "2025-10-05"),
        ("1 Des 2025", "2025-12-01"),
        ("17 Mei 2026", "2026-05-17"),
        ("12 August 2024", "2024-08-12"),  # English spellings on chain receipts
        ("21 Jun 2026", "2026-06-21"),
        ("14 Agustus 2026", "2026-08-14"),
    ],
)
def test_day_first_month_names(text, expected):
    assert extract_date(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("16/06/2026", "2026-06-16"),
        ("16-06-2026", "2026-06-16"),
        ("16.06.2026", "2026-06-16"),
        ("03/04/2026", "2026-04-03"),  # ambiguous: the Indonesian reading wins
        ("16/06/26", "2026-06-16"),
        ("16/06/99", "1999-06-16"),
    ],
)
def test_numeric_dates_are_day_first(text, expected):
    assert extract_date(text) == expected


def test_us_order_is_only_a_fallback_when_day_first_is_impossible():
    assert extract_date("12/25/2026") == "2026-12-25"
    assert extract_date("03/04/2026") == "2026-04-03"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no date here",
        "31/02/2026",  # not a day in February, and not a US month either
        "00/00/2026",
        "2026-13-45",
        "16/06/75",  # before the 1990 floor
        "Total 44.000",
    ],
)
def test_unusable_dates_are_none(text):
    assert extract_date(text) is None


def test_iso_is_preferred_over_a_numeric_date_elsewhere_in_the_text():
    assert extract_date("Tgl 14/07/2026 ... ref 2026-01-02") == "2026-01-02"


def test_an_invalid_match_does_not_hide_a_later_valid_one():
    assert extract_date("99/99/2026 then 16/06/2026") == "2026-06-16"
