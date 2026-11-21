"""Phase-2 value and quality algorithms. Registered now so the catalogue is complete; each reports what it needs."""
from __future__ import annotations

from ier.algos.core import AlgoResult
from ier.algos.registry import Algorithm, register

_PHASE2 = [
    ("pe_ratio", "Price to earnings", "needs trailing four-quarter EPS", "Price divided by trailing twelve-month earnings per share."),
    ("earnings_yield", "Earnings yield", "needs trailing four-quarter EPS", "Trailing earnings per share divided by price."),
    ("peg", "PEG ratio", "needs trailing EPS and an earnings growth rate", "Price to earnings divided by earnings growth."),
    ("piotroski_f", "Piotroski F-score", "needs two years of statements (profit, cash flow, leverage, shares, margins)", "Nine yes/no financial-health checks."),
    ("altman_z", "Altman Z-score", "needs balance sheet and income statement totals", "Distress score from working capital, retained earnings, profit, equity and sales ratios."),
    ("magic_formula", "Magic formula rank", "needs EBIT, enterprise value and capital employed for the sector", "Combined rank of earnings yield and return on capital."),
    ("dupont_roe", "DuPont return on equity", "needs net profit, sales, assets and equity", "Return on equity split into margin, turnover and leverage."),
    ("graham_number", "Graham number", "needs earnings per share and book value per share", "Square root of 22.5 x EPS x book value per share, compared with price."),
]


def _stub(need_text: str):
    return lambda ctx: AlgoResult(None, None, need_text)


for _id, _name, _need, _desc in _PHASE2:
    register(Algorithm(_id, _name, "value_quality", "long", 0, _desc, _stub(_need), None))
