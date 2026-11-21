"""Reporting period labels -> period end date (Indian fiscal year, April to March).

Used so the rules engine picks the latest fact by the period it reports, never by the order
facts were inserted (a results table lists the current year next to prior-year comparatives).

Accepted forms (case and spacing ignored):
  FY2025 / FY25 / FY 2025            -> 31 Mar 2025 (fiscal year ending March of that year)
  FY2024-25 / FY 2024-25 / 2024-25   -> 31 Mar 2025
  Q1FY27 .. Q4FY27 (also Q2 FY2027)  -> 30 Jun 2026, 30 Sep 2026, 31 Dec 2026, 31 Mar 2027
  H1FY27 / H2FY27                    -> 30 Sep 2026 / 31 Mar 2027
  9MFY27                             -> 31 Dec 2026
  CY2025                             -> 31 Dec 2025
  2025-03-31 (ISO date)              -> that date
  "31 March 2025", "Mar 2025", "Sep-2026", "quarter ended 30 September 2026" -> month end
Anything else (e.g. "Q1" without a year, "TTM", "latest") -> None: the period is unknown.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
_MONTHS["sept"] = 9
# fiscal-year relative end month and year offset: Q1 Apr-Jun ends Jun of FY start year, ...
_QUARTER_END = {1: (6, -1), 2: (9, -1), 3: (12, -1), 4: (3, 0)}
_HALF_END = {1: (9, -1), 2: (3, 0)}


def _year(text: str) -> int:
    y = int(text)
    return 2000 + y if y < 100 else y


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_period_end(period: str | None) -> date | None:
    """Period end date for a reporting period label, or None if it cannot be determined."""
    if not isinstance(period, str):
        return None
    p = re.sub(r"\s+", " ", period.strip().lower())
    if not p:
        return None
    compact = p.replace(" ", "").replace("'", "")
    try:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", compact)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"q([1-4])fy(\d{2}|\d{4})", compact)
        if m:
            month, offset = _QUARTER_END[int(m[1])]
            return _month_end(_year(m[2]) + offset, month)
        m = re.fullmatch(r"h([12])fy(\d{2}|\d{4})", compact)
        if m:
            month, offset = _HALF_END[int(m[1])]
            return _month_end(_year(m[2]) + offset, month)
        m = re.fullmatch(r"9mfy(\d{2}|\d{4})", compact)
        if m:
            return date(_year(m[1]) - 1, 12, 31)
        m = re.fullmatch(r"(?:fy)?(\d{4})-(\d{2}|\d{4})", compact)
        if m:
            start, end = int(m[1]), _year(m[2]) if len(m[2]) == 4 else int(m[1][:2] + m[2])
            return date(end, 3, 31) if end == start + 1 else None
        m = re.fullmatch(r"fy(\d{2}|\d{4})", compact)
        if m:
            return date(_year(m[1]), 3, 31)
        m = re.fullmatch(r"cy(\d{4})", compact)
        if m:
            return date(int(m[1]), 12, 31)
        # "31 march 2025", "mar 2025", "sep-2026", "quarter ended 30 september 2026"
        m = re.search(r"(?:(\d{1,2})[ -])?([a-z]{3,9})[ ,-]*(\d{4})$", p)
        if m and m[2] in _MONTHS:
            year, month = int(m[3]), _MONTHS[m[2]]
            if m[1]:
                return date(year, month, int(m[1]))
            return _month_end(year, month)
    except ValueError:  # impossible date such as 2025-02-30
        return None
    return None
