"""Input normalization and validation shared by the API and agent tools.

The LLM is never trusted to validate. Everything a patient types goes through
these helpers, and the tools return structured ``field_errors`` the model can
relay verbatim. Messages are written for a patient, not for a developer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.clinic import ACCEPTED_INSURANCE_PLANS, SELF_PAY_LABEL

MAX_AGE_YEARS = 120

_NAME_ALLOWED = re.compile(r"^[A-Za-z\u00C0-\u024F][A-Za-z\u00C0-\u024F .'\-]*$")
_HAS_LETTER = re.compile(r"[A-Za-z\u00C0-\u024F]")

_PHONE_JUNK = re.compile(r"[^\d+]")


# --------------------------------------------------------------------------- #
# Phone numbers (US / NANP only)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PhoneResult:
    ok: bool
    e164: str | None = None
    national: str | None = None
    error: str | None = None


def normalize_phone(raw: str | None) -> PhoneResult:
    """Normalize a US number to E.164, rejecting anything ambiguous.

    Accepts ``6505550147``, ``(650) 555-0147``, ``650.555.0147``,
    ``+1 650 555 0147``, ``1-650-555-0147``. Rejects extensions, short codes,
    and non-NANP country codes with a specific, patient-friendly reason.
    """
    if raw is None:
        return PhoneResult(False, error="I still need a phone number.")
    text = raw.strip()
    if not text:
        return PhoneResult(False, error="I still need a phone number.")
    if re.search(r"\b(ext|x|extension)\b\.?\s*\d+", text, re.IGNORECASE):
        return PhoneResult(
            False, error="Please give just the 10-digit number, without an extension."
        )

    digits = _PHONE_JUNK.sub("", text)
    had_plus = digits.startswith("+")
    digits = digits.lstrip("+")

    if had_plus and not digits.startswith("1"):
        return PhoneResult(
            False, error="That looks like a non-US number. Please give a US phone number."
        )
    if len(digits) == 11:
        if not digits.startswith("1"):
            return PhoneResult(False, error="Please give a 10-digit US phone number.")
        digits = digits[1:]
    elif len(digits) != 10:
        return PhoneResult(
            False, error="That phone number doesn't look complete. I need 10 digits."
        )

    area, prefix, line = digits[:3], digits[3:6], digits[6:]
    if area[0] in "01" or prefix[0] in "01":
        return PhoneResult(False, error="That doesn't look like a valid US phone number.")

    return PhoneResult(
        ok=True,
        e164=f"+1{digits}",
        national=f"({area}) {prefix}-{line}",
    )


def phone_digits(value: str | None) -> str:
    """Last 10 digits, used for loose lookups against stored numbers."""
    if not value:
        return ""
    return re.sub(r"\D", "", value)[-10:]


# --------------------------------------------------------------------------- #
# Date of birth
# --------------------------------------------------------------------------- #

_NUMERIC_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ISO: 1984-03-12 / 1984/03/12. Not anchored — matched first so a value like
    # "1984-03-12" is read year-first regardless of separators.
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), "ymd"),
    # US: 03/12/1984, 3-12-84. Only tried when the first component cannot be a
    # year of birth (i.e. exactly 2 digits), so "12/03/1984" is not misread.
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$"), "mdy"),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2})$"), "mdy"),
)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_TEXT_DATE = re.compile(
    r"^(?:([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})"
    r"|(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)\.?,?\s+(\d{4}))$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DateResult:
    ok: bool
    value: date | None = None
    iso: str | None = None
    error: str | None = None
    code: str | None = None
    ambiguous: bool = False


def _expand_year(year: int) -> int:
    """Two-digit years: 00-29 -> 2000s, 30-99 -> 1900s (dob convention)."""
    if year >= 100:
        return year
    return 2000 + year if year <= 29 else 1900 + year


def parse_date_of_birth(raw: str | None, *, today: date | None = None) -> DateResult:
    """Parse and validate a DOB. Dates are clinic-local calendar dates."""
    if raw is None or not raw.strip():
        return DateResult(False, code="missing", error="I still need the date of birth.")

    text = raw.strip().lower().replace(",", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    current = today or date.today()

    parsed: tuple[int, int, int] | None = None
    day_month_ambiguous = False

    for pattern, kind in _NUMERIC_DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        first, second, third = (int(value) for value in match.groups())
        if kind == "ymd":
            parsed = (first, second, third)
        else:
            year = _expand_year(third)
            month, day = first, second
            if month > 12:
                # First component can't be a month. If the second can be, the
                # caller probably wrote DD/MM — accept it, but flag it so the
                # assistant confirms (identity matching keys on the exact DOB).
                if day <= 12:
                    parsed = (year, day, month)
                    day_month_ambiguous = True
                    break
                return DateResult(
                    False,
                    code="impossible",
                    error=(
                        "That doesn't look like a valid month and day. Could you write the "
                        "date of birth as MM/DD/YYYY?"
                    ),
                )
            if first <= 12 and second <= 12 and first != second:
                # e.g. "03/05/1984": valid both ways. Keep the US month/day
                # reading (the clinic's locale) but flag it for confirmation.
                day_month_ambiguous = True
            parsed = (year, month, day)
        break

    if parsed is None:
        match = _TEXT_DATE.match(text)
        if match:
            month_word = match.group(1) or match.group(5)
            day_text = match.group(2) or match.group(4)
            year_text = match.group(3) or match.group(6)
            month = _MONTHS.get((month_word or "").lower())
            if month is None:
                return DateResult(
                    False,
                    code="invalid",
                    error="I couldn't read that month. Could you repeat the date of birth?",
                )
            parsed = (int(year_text), month, int(day_text))

    if parsed is None:
        return DateResult(
            False,
            code="unparsed",
            error="I couldn't read that date. Could you give the date of birth as MM/DD/YYYY?",
        )

    year, month, day = parsed
    try:
        value = date(year, month, day)
    except ValueError:
        return DateResult(
            False,
            code="impossible",
            error="That date doesn't exist on the calendar. Could you check it?",
        )

    if value > current:
        return DateResult(
            False,
            code="future",
            error="That date is in the future, so it can't be a date of birth.",
        )
    if value < current - timedelta(days=365 * MAX_AGE_YEARS):
        return DateResult(False, code="too_old", error="Could you double-check the year of birth?")
    if value == current:
        return DateResult(
            False,
            code="today",
            error="Could you double-check the date of birth? It looks like today.",
        )

    return DateResult(True, value=value, iso=value.isoformat(), ambiguous=day_month_ambiguous)


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NameResult:
    ok: bool
    value: str | None = None
    first_name: str | None = None
    error: str | None = None


def normalize_name(raw: str | None) -> NameResult:
    if raw is None or not raw.strip():
        return NameResult(False, error="I still need a full name.")
    collapsed = re.sub(r"\s+", " ", raw.strip())
    if len(collapsed) > 120:
        return NameResult(
            False,
            error=(
                "That name is too long. Could you give the name as it should appear on the chart?"
            ),
        )
    if not _NAME_ALLOWED.match(collapsed):
        return NameResult(
            False,
            error=(
                "Names can use letters, spaces, hyphens, apostrophes, and periods. "
                "Could you give the full name?"
            ),
        )
    if not _HAS_LETTER.search(collapsed):
        return NameResult(False, error="I need the patient's full name, letters only.")
    if not re.search(r"\s", collapsed):
        return NameResult(
            False,
            error="Could I get the first and last name together, so I can find the right chart?",
        )
    words = [word for word in re.split(r"\s+", collapsed)]
    if any(len(word.strip(". ")) < 1 for word in words):
        return NameResult(False, error="Could you give the full name?")
    normalized = " ".join(capitalize_name_word(word) for word in words)
    return NameResult(True, value=normalized, first_name=normalized.split(" ")[0])


def capitalize_name_word(word: str) -> str:
    """Capitalize hyphenated parts but preserve apostrophe style.

    ``o'neil`` -> ``O'neil`` (not ``O'Neil``); ``smith-jones`` -> ``Smith-Jones``.
    """
    parts = [part.capitalize() if part else part for part in word.split("-")]
    return "-".join(parts)


# --------------------------------------------------------------------------- #
# Insurance
# --------------------------------------------------------------------------- #

# Phrases (not substrings) that mean "no insurance".
_SELF_PAY_PHRASES = (
    "self pay",
    "self-pay",
    "selfpay",
    "no insurance",
    "don't have insurance",
    "dont have insurance",
    "without insurance",
    "pay cash",
    "out of pocket",
)
_SELF_PAY_EXACT = {"none", "nothing", "neither", "n/a", "na", "cash"}

_OTHER_PLAN_WORDS = ("other", "another", "different")


@dataclass(frozen=True)
class InsuranceResult:
    ok: bool
    value: str | None = None
    is_self_pay: bool = False
    error: str | None = None


def normalize_insurance(raw: str | None) -> InsuranceResult:
    """Map free text to an accepted plan, ``Self-pay``, or ``Other plan``.

    Unknown plans are stored under the patient's own wording rather than
    rejected: the front desk verifies coverage later, and a patient should never
    be blocked at registration because their plan is unfamiliar.
    """
    if raw is None or not raw.strip():
        return InsuranceResult(
            False, error="I still need an insurance provider, or say self-pay."
        )

    text = re.sub(r"\s+", " ", raw.strip())
    lowered = text.lower()
    compact = re.sub(r"[^a-z]", "", lowered)

    if lowered in _SELF_PAY_EXACT or any(phrase in lowered for phrase in _SELF_PAY_PHRASES):
        return InsuranceResult(True, value=SELF_PAY_LABEL, is_self_pay=True)

    for plan in ACCEPTED_INSURANCE_PLANS:
        if re.sub(r"[^a-z]", "", plan.name.lower()) == compact:
            return InsuranceResult(True, value=plan.name)
        # Match distinctive words ("sunnycoast", "blue harbor", "medical") without
        # requiring the patient to say "Dental PPO".
        plan_words = [word for word in plan.name.lower().split() if word not in {"dental", "ppo"}]
        if plan_words and all(word in lowered for word in plan_words):
            return InsuranceResult(True, value=plan.name)

    if any(word in lowered for word in _OTHER_PLAN_WORDS):
        return InsuranceResult(True, value="Other plan")

    return InsuranceResult(True, value=text[:120])
