"""F1: reporting periods parse to a period end date (Indian fiscal year: April-March)."""
from datetime import date

import pytest

from ier.rules.periods import parse_period_end


@pytest.mark.parametrize("text,expected", [
    ("FY2025", date(2025, 3, 31)),
    ("FY25", date(2025, 3, 31)),
    ("FY 2026", date(2026, 3, 31)),
    ("fy26", date(2026, 3, 31)),
    ("FY2024-25", date(2025, 3, 31)),
    ("FY 2025-26", date(2026, 3, 31)),
    ("2025-26", date(2026, 3, 31)),
    ("Q1FY27", date(2026, 6, 30)),
    ("Q2FY27", date(2026, 9, 30)),
    ("Q3 FY27", date(2026, 12, 31)),
    ("Q4FY26", date(2026, 3, 31)),
    ("Q2 FY2027", date(2026, 9, 30)),
    ("H1FY27", date(2026, 9, 30)),
    ("H2 FY27", date(2027, 3, 31)),
    ("9MFY27", date(2026, 12, 31)),
    ("2025-03-31", date(2025, 3, 31)),
    ("31 March 2025", date(2025, 3, 31)),
    ("Mar 2025", date(2025, 3, 31)),
    ("Sep-2026", date(2026, 9, 30)),
    ("quarter ended 30 September 2026", date(2026, 9, 30)),
    ("CY2025", date(2025, 12, 31)),
])
def test_parses_known_period_forms(text, expected):
    assert parse_period_end(text) == expected


@pytest.mark.parametrize("text", ["", "Q1", "latest", "TTM", None, "FY", "Q5FY27", "FY2024-27",
                                  "2025-02-30"])
def test_unparseable_periods_are_none(text):
    assert parse_period_end(text) is None
