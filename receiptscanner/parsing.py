"""Turn raw OCR text into suggested field values for the review form.

Tuned for Indonesian receipts: `1.234.567,89` number formatting, Indonesian
month names, and the usual POS vocabulary (TOTAL BAYAR, TUNAI, KEMBALI...).
Every suggestion is a starting point the user can overwrite in the UI.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

FALLBACK_CATEGORY = "Lainnya"

MONTHS: dict[str, int] = {
    "januari": 1,
    "februari": 2,
    "maret": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "agustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "desember": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "agu": 8,
    "ags": 8,
    "agt": 8,
    "sep": 9,
    "sept": 9,
    "okt": 10,
    "nov": 11,
    "des": 12,
    # English spellings show up on chain-store receipts.
    "january": 1,
    "february": 2,
    "march": 3,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "october": 10,
    "december": 12,
    "aug": 8,
    "oct": 10,
    "dec": 12,
}

# Higher tier wins. Within a tier the largest amount wins.
TOTAL_KEYWORDS: list[tuple[int, tuple[str, ...]]] = [
    (
        3,
        (
            "grand total",
            "total bayar",
            "total belanja",
            "total penjualan",
            "total akhir",
            "total tagihan",
            "jumlah bayar",
            "total pembayaran",
            "total amount",
            "amount due",
            "total paid",
            "total harga",
            "order total",
            "total charged",
            "amount charged",
            "amount paid",
            "balance due",
            "total due",
        ),
    ),
    (2, ("total", "jumlah", "netto", "net sales", "net total")),
    (
        1,
        (
            "subtotal",
            "sub total",
            "tunai",
            "cash",
            "bayar",
            "dibayar",
            "debit",
            "kartu kredit",
            "credit card",
            "rp",
            "usd",
        ),
    ),
]

# Lines that carry a number which is never the receipt total.
AMOUNT_EXCLUSIONS = (
    "kembali",
    "kembalian",
    "change",
    "diskon",
    "discount",
    "potongan",
    "hemat",
    "saving",
    "ppn",
    "pajak",
    "tax",
    "service charge",
    "poin",
    "point",
    "npwp",
    "no.",
    "telp",
    "phone",
    "kasir",
    "cashier",
    "member",
)

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Makanan & Minuman": (
        "resto",
        "restoran",
        "cafe",
        "kafe",
        "coffee",
        "kopi",
        "warung",
        "warteg",
        "bakso",
        "ayam",
        "nasi",
        "mie",
        "food",
        "burger",
        "pizza",
        "kfc",
        "mcd",
        "mcdonald",
        "starbucks",
        "bakery",
        "roti",
        "dapur",
        "kitchen",
        "catering",
        "sate",
        "soto",
        "seafood",
        "dimsum",
        "boba",
        "juice",
        "es teh",
        "geprek",
        "cappucino",
        "cappuccino",
        "espresso",
        "latte",
        "croissant",
        "caffeine",
        "eatery",
        "bistro",
        "grill",
        "chicken",
        "kedai",
        "bakmi",
        "kitchen",
        "grabfood",
        "gofood",
        "shopeefood",
        "makanan",
        "minuman",
        "menikmati",
    ),
    "Belanja Harian": (
        "indomaret",
        "alfamart",
        "alfamidi",
        "superindo",
        "hypermart",
        "transmart",
        "carrefour",
        "hero",
        "ranch market",
        "supermarket",
        "minimarket",
        "grosir",
        "swalayan",
        "toserba",
        "lotte",
        "grocery",
    ),
    "Transportasi": (
        "grab",
        "gojek",
        "gocar",
        "goride",
        "bluebird",
        "blue bird",
        "taksi",
        "taxi",
        "parkir",
        "parking",
        "bensin",
        "pertamina",
        "shell",
        "spbu",
        "bbm",
        "pertalite",
        "pertamax",
        "tol",
        "kereta",
        "krl",
        "mrt",
        "lrt",
        "busway",
        "transjakarta",
        "damri",
        "bengkel",
    ),
    "Kesehatan": (
        "apotek",
        "apotik",
        "kimia farma",
        "guardian",
        "century",
        "klinik",
        "rumah sakit",
        "dokter",
        "obat",
        "laboratorium",
        "pharmacy",
        "hospital",
        "medical",
        "dental",
        "gigi",
    ),
    "Tagihan & Utilitas": (
        "pln",
        "listrik",
        "pdam",
        "indihome",
        "telkom",
        "wifi",
        "pulsa",
        "token listrik",
        "biznet",
        "first media",
        "myrepublic",
        "internet",
        "tagihan",
        "iuran",
    ),
    "Perjalanan & Akomodasi": (
        "hotel",
        "airbnb",
        "garuda",
        "lion air",
        "citilink",
        "airasia",
        "batik air",
        "traveloka",
        "tiket.com",
        "penginapan",
        "villa",
        "resort",
        "bandara",
        "boarding pass",
        "guest house",
    ),
    "Hiburan": (
        "bioskop",
        "cinema",
        "cgv",
        "cinepolis",
        "xxi",
        "netflix",
        "spotify",
        "karaoke",
        "game",
        "steam",
        "playstation",
        "wahana",
        "tiket masuk",
    ),
    "Perlengkapan Kantor": (
        "atk",
        "gramedia",
        "stationery",
        "percetakan",
        "fotokopi",
        "toner",
        "tinta",
        "office",
        "kertas a4",
        "alat tulis",
    ),
    "Elektronik": (
        "erafone",
        "ibox",
        "digimap",
        "elektronik",
        "laptop",
        "komputer",
        "handphone",
        "samsung",
        "xiaomi",
        "electronic city",
    ),
}

# Header words that are never the merchant name.
NAME_NOISE = (
    "struk",
    "nota",
    "invoice",
    "receipt",
    "faktur",
    "kwitansi",
    "bukti",
    "customer copy",
    "merchant copy",
    "npwp",
    "terima kasih",
    "thank you",
    "welcome",
    "selamat datang",
    "tax invoice",
    "pembayaran",
)

_MONEY_TOKEN = re.compile(r"\d[\d.,]*\d|\d")

# A dollar is "$" directly before a digit, "US$", or the word USD. The look-
# arounds keep "S$ 5" (Singapore), "BUSD" and "USDA" from counting.
_USD_MARKER = re.compile(
    r"(?<![A-Za-z])US\$|(?<![A-Za-z])USD(?![A-Za-z])|(?<![A-Za-z$])\$(?=\s?\d)",
    re.IGNORECASE,
)
_IDR_MARKER = re.compile(r"(?<![A-Za-z])(?:Rp|IDR|rupiah)(?![A-Za-z])", re.IGNORECASE)
_HAS_CENTS = re.compile(r"[.,]\d{1,2}$")

# IDR totals are in the thousands; anything below this is a quantity or count.
IDR_FLOOR = Decimal(100)


@dataclass
class ParsedReceipt:
    entry_date: str = ""
    category: str = ""
    name: str = ""
    amount: str = ""
    # "" when nothing was printed to decide it and no default was supplied.
    currency: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "entry_date": self.entry_date,
            "category": self.category,
            "name": self.name,
            "amount": self.amount,
        }


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    # OCR routinely renders Rp as R p / RP. and confuses O/0 in headers.
    return text.replace(" ", " ")


def detect_currency(text: str, default: str = "") -> str:
    """Which currency a receipt is printed in: "USD", "IDR", or `default`.

    Counts the markers of each and takes the more frequent, so a rupiah receipt
    that mentions one dollar fare is still rupiah. A tie, or no marker at all,
    is undecided and returns `default` rather than guessing.
    """
    text = _normalize(text or "")
    dollars = len(_USD_MARKER.findall(text))
    rupiah = len(_IDR_MARKER.findall(text))
    if dollars > rupiah:
        return "USD"
    if rupiah > dollars:
        return "IDR"
    return default


def parse_number(raw: str) -> Decimal | None:
    """Parse a money token under either Indonesian or English separators.

    `10.000` -> 10000, `1.234,56` -> 1234.56, `1,234.56` -> 1234.56.
    The decision hinges on the digit count after the last separator: a group of
    three is a thousands separator, one or two digits is a decimal fraction.
    """
    cleaned = re.sub(r"[^\d.,]", "", raw or "")
    if not cleaned or not re.search(r"\d", cleaned):
        return None

    last_sep = max(cleaned.rfind("."), cleaned.rfind(","))
    try:
        if last_sep == -1:
            return Decimal(cleaned)

        tail = cleaned[last_sep + 1 :]
        if len(tail) in (1, 2) and tail.isdigit():
            head = re.sub(r"[.,]", "", cleaned[:last_sep]) or "0"
            return Decimal(f"{head}.{tail}")
        return Decimal(re.sub(r"[.,]", "", cleaned) or "0")
    except (InvalidOperation, ArithmeticError):
        return None


def _money_tokens(
    line: str, money_shaped_only: bool = False
) -> list[tuple[str, Decimal]]:
    """(printed token, value) for each number on a line.

    `money_shaped_only` drops bare digit runs. Receipts are full of long
    unformatted numbers - card numbers, merchant and approval codes, phone
    numbers - that dwarf any real total. Printed amounts almost always carry a
    group separator, so requiring one (or a short run of digits) keeps
    identifiers out of the running.
    """
    found = []
    for match in _MONEY_TOKEN.finditer(line):
        token = match.group(0)
        value = parse_number(token)
        if value is None or value <= 0:
            continue
        if money_shaped_only:
            separators = token.count(".") + token.count(",")
            digits = sum(character.isdigit() for character in token)
            if separators == 0 and digits > 6:
                continue
            if digits > 12:
                continue
        found.append((token, value))
    return found


def _money_candidates(line: str, money_shaped_only: bool = False) -> list[Decimal]:
    """Numbers on a line. See `_money_tokens` for what money-shaped means."""
    return [value for _, value in _money_tokens(line, money_shaped_only)]


def _line_amounts(line: str, currency: str) -> list[Decimal]:
    """The values on a line that could plausibly be a money amount.

    IDR drops anything under the floor: a rupiah total is never that small, so
    small numbers are quantities. A dollar total can be 0.99, so that floor
    would erase it; instead a dollar value must carry cents or sit on a line
    that names the currency, which still rejects a bare "Total 3".
    """
    tokens = _money_tokens(line, money_shaped_only=True)
    if currency != "USD":
        return [value for _, value in tokens if value >= IDR_FLOOR]
    named = bool(_USD_MARKER.search(line))
    return [value for token, value in tokens if named or _HAS_CENTS.search(token)]


def format_amount(value: Decimal | float | None) -> str:
    if value is None:
        return ""
    decimal_value = Decimal(str(value))
    if decimal_value == decimal_value.to_integral_value():
        return str(int(decimal_value))
    return f"{decimal_value:.2f}"


def extract_amount(text: str, currency: str = "") -> Decimal | None:
    currency = currency or detect_currency(text) or "IDR"
    best_tier = 0
    best_value: Decimal | None = None
    unlabelled: list[Decimal] = []

    for line in _normalize(text).splitlines():
        lowered = line.lower().strip()
        if not lowered or any(bad in lowered for bad in AMOUNT_EXCLUSIONS):
            continue

        # Longest matching keyword decides the tier, not the first one found:
        # "subtotal" contains "total", so scanning tiers in order would rank a
        # subtotal line as if it were the grand total.
        tier = 0
        matched = ""
        for level, keywords in TOTAL_KEYWORDS:
            for keyword in keywords:
                if keyword in lowered and len(keyword) > len(matched):
                    tier, matched = level, keyword

        values = _line_amounts(line, currency)
        if not values:
            continue
        candidate = max(values)

        if tier == 0:
            unlabelled.append(candidate)
            continue

        if tier > best_tier or (
            tier == best_tier and best_value is not None and candidate > best_value
        ):
            best_tier, best_value = tier, candidate

    if best_value is None:
        # Every label was unreadable: take the largest money-shaped number.
        return max(unlabelled) if unlabelled else None

    # A modestly larger amount on an unlabelled line usually means OCR mangled
    # the label on the real total ("Grand Total" -> "Irand Total") while the
    # payment line below it survived. Bounded so a stray figure cannot take over.
    if unlabelled:
        largest = max(unlabelled)
        if best_value < largest <= best_value * Decimal("1.5"):
            return largest

    return best_value


def _valid_date(year: int, month: int, day: int) -> str | None:
    if year < 100:
        year += 2000 if year < 70 else 1900
    if not (1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def extract_date(text: str, currency: str = "") -> str | None:
    text = _normalize(text)
    currency = currency or detect_currency(text) or "IDR"

    # ISO first, it is unambiguous.
    for match in re.finditer(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", text):
        parsed = _valid_date(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        )
        if parsed:
            return parsed

    # 12 Agustus 2024 / 12-Agu-24 / "03 JUN,26" (OCR often reads "." as ",")
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    pattern = rf"\b(\d{{1,2}})[\s\-/.,]*({month_names})[\s\-/.,]*(\d{{2,4}})\b"
    for match in re.finditer(pattern, text, re.IGNORECASE):
        month = MONTHS[match.group(2).lower()]
        parsed = _valid_date(int(match.group(3)), month, int(match.group(1)))
        if parsed:
            return parsed

    # "Aug 17, 2026" / "July 14th 2026": unambiguous, so no currency needed.
    month_first = (
        rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(\d{{4}})\b"
    )
    for match in re.finditer(month_first, text, re.IGNORECASE):
        month = MONTHS[match.group(1).lower()]
        parsed = _valid_date(int(match.group(3)), month, int(match.group(2)))
        if parsed:
            return parsed

    # Numeric. Day-first is the Indonesian convention; a dollar receipt is
    # month-first. Whichever order is impossible for a given date (a month of
    # 25) falls back to the other rather than dropping the date entirely.
    for match in re.finditer(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b", text):
        first, second, year = (int(g) for g in match.groups())
        if currency == "USD":
            orders = ((first, second), (second, first))  # (month, day)
        else:
            orders = ((second, first), (first, second))
        for month, day in orders:
            parsed = _valid_date(year, month, day)
            if parsed:
                return parsed
    return None


def extract_name(text: str) -> str | None:
    """Merchant name: the first substantial line of the header block."""
    lines = [line.strip(" *=-_|.,:'\"") for line in _normalize(text).splitlines()]
    candidates: list[tuple[int, str]] = []

    for index, line in enumerate(lines[:10]):
        if len(line) < 3:
            continue
        lowered = line.lower()
        if any(noise in lowered for noise in NAME_NOISE):
            continue

        letters = sum(character.isalpha() for character in line)
        if letters < 3 or letters < len(line) * 0.5:
            continue  # mostly digits/punctuation: an address, phone or barcode
        if re.search(r"\b(jl|jalan|telp|tlp|phone|no\.?\s*\d)\b", lowered):
            continue

        tokens = line.split()
        if not any(len(token) >= 4 and token.isalpha() for token in tokens):
            continue  # scattered short fragments: OCR noise, not a shop name
        # Runs of one- and two-letter tokens are the signature of a garbled line.
        stubs = sum(1 for token in tokens if len(token) <= 2)
        if tokens and stubs / len(tokens) > 0.4:
            continue

        # Prefer lines near the top, but not so strongly that a garbled first
        # line outranks the real name printed just below it.
        score = (10 - index) * 5 + min(len(line), 40)
        if line.isupper():
            score += 20  # store names are nearly always printed in caps
        candidates.append((score, line))

    if not candidates:
        return None
    return max(candidates)[1][:120]


def guess_category(text: str, allowed: list[str]) -> str:
    lowered = _normalize(text).lower()
    scores: dict[str, int] = {}

    # Score by total matched keyword length, not by number of matches, so a
    # specific term outweighs an incidental one: a Grab ride receipt that was
    # picked up outside a KFC should stay Transportasi, while "grabfood" beats
    # the bare "grab" it contains.
    for category, keywords in CATEGORY_KEYWORDS.items():
        if category not in allowed:
            continue
        score = sum(len(keyword) for keyword in keywords if keyword in lowered)
        if score:
            scores[category] = score

    if scores:
        return max(scores.items(), key=lambda item: item[1])[0]
    if FALLBACK_CATEGORY in allowed:
        return FALLBACK_CATEGORY
    return allowed[-1] if allowed else ""


def parse_receipt(
    text: str,
    categories: list[str],
    fallback_date: str = "",
    default_currency: str = "",
) -> ParsedReceipt:
    if not text or not text.strip():
        return ParsedReceipt(
            entry_date=fallback_date, category=guess_category("", categories)
        )

    # What the receipt prints beats the configured default, which only fills in
    # when nothing was printed to decide it.
    currency = detect_currency(text, default_currency)
    amount = extract_amount(text, currency)
    return ParsedReceipt(
        entry_date=extract_date(text, currency) or fallback_date,
        category=guess_category(text, categories),
        name=extract_name(text) or "",
        amount=format_amount(amount),
        currency=currency,
    )
