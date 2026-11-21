"""Relative-strength algorithms: against the sector index and against sector peers. Bands are PLACEHOLDERs."""
from __future__ import annotations

from ier.algos._common import DEFAULT_POINTS, P, S, band_for, days, empty_chart, make_algo, need
from ier.algos.core import AlgoResult, Ctx, linear_score
from ier.algos.registry import Algorithm, register


def _aligned(ctx: Ctx, lookback: int):
    """[(day, company_close, index_close)] over the company's last lookback+1 days that the index also has."""
    idx = {b.day: b.close for b in ctx.index_bars or []}
    win = ctx.bars[-(lookback + 1):]
    return [(b.day, b.close, idx[b.day]) for b in win if b.day in idx], len(win)


def _rs_algo(id, name, horizon, lookback, band, desc):
    def value(c, ctx):
        if not ctx.index_bars:
            return AlgoResult(None, None, "no index data for this sector")
        rows, n = _aligned(ctx, lookback)
        if len(rows) < 0.8 * n or len(rows) < 2:
            return AlgoResult(None, None, "index dates do not line up")
        co = (rows[-1][1] / rows[0][1] - 1) * 100
        ix = (rows[-1][2] / rows[0][2] - 1) * 100
        return co - ix, f"company {co:.1f}% vs index {ix:.1f}%"

    def chart(ctx, c):
        idx = {b.day: b.close for b in ctx.index_bars or []}
        k = min(int(ctx.cfg.get("points", DEFAULT_POINTS)), len(c))
        start = len(c) - k
        d = ctx.bars
        base_c = c[start]
        base_i = next((idx[b.day] for b in d[start:] if b.day in idx), None)
        co = [x / base_c * 100 for x in c]
        ix = [idx[b.day] / base_i * 100 if base_i and b.day in idx else None for b in d]
        ratio = [None if i is None else a / i * 100 for a, i in zip(co, ix)]
        return ([P("Rebased to 100 at the start", "line", [S("Company", co), S("Sector index", ix, "dashed")]),
                 P("Company relative to index", "line", [S("Ratio", ratio), S("Level at start", [100.0] * len(c), "dashed")])],
                [], "Company and sector index, both set to 100 at the start; the lower panel is their ratio.")

    make_algo(id=id, name=name, family="relative", horizon=horizon, min_history=lookback + 1, description=desc,
              value_fn=value, band=band, chart_fn=chart, unit=" points")


_rs_algo("rs_vs_sector_index_3m", "Return vs sector index, 3 months", "short", 63, (-15, 15),
         "Company's 63-day return minus the sector index's over the same dates, in percentage points.")
_rs_algo("rs_vs_sector_index_12m", "Return vs sector index, 12 months", "long", 252, (-25, 25),
         "Company's 252-day return minus the sector index's over the same dates, in percentage points.")


def _own_mom6(ctx):
    c = [b.close for b in ctx.bars]
    return (c[-1] / c[-127] - 1) * 100


def _rank_compute(ctx: Ctx) -> AlgoResult:
    short = need(ctx, 127)
    if short:
        return short
    peers = dict(ctx.peer_momentum or {})
    peers[ctx.isin] = peers.get(ctx.isin, _own_mom6(ctx))
    if len(peers) < 3:
        return AlgoResult(None, None, "needs 3 peers in the sector")
    me = peers[ctx.isin]
    below = sum(1 for k, v in peers.items() if k != ctx.isin and v < me)
    pct = below / (len(peers) - 1) * 100
    return AlgoResult(round(linear_score(pct, *band_for(ctx, "rs_rank_in_sector", (0, 100))), 2), round(pct, 4),
                      f"6-month return ahead of {pct:.0f}% of {len(peers) - 1} sector peers")


def _rank_chart(ctx: Ctx):
    if len(ctx.bars) < 127:
        return empty_chart(127)
    peers = dict(ctx.peer_momentum or {})
    peers[ctx.isin] = peers.get(ctx.isin, _own_mom6(ctx))
    if len(peers) < 3:
        return {"dates": [], "panels": [], "markers": [], "caption": "needs 3 peers in the sector", "as_of": None}
    order = sorted(peers.items(), key=lambda kv: kv[1])
    return {"dates": [k for k, _ in order],
            "panels": [P("6-month return by company (%)", "bars", [S("6-month return", [v for _, v in order], "bar")])],
            "markers": [{"day": ctx.isin, "label": "this company"}],
            "caption": "6-month return of every company in the sector, lowest to highest; the marked bar is this company.",
            "as_of": ctx.bars[-1].day.isoformat()}


register(Algorithm("rs_rank_in_sector", "Rank among sector peers, 6 months", "relative", "long", 127,
                   "Share of sector peers whose 6-month return is below this company's, in percent.", _rank_compute, _rank_chart, (0, 100)))
