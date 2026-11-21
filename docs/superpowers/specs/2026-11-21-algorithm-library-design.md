# Algorithm library: design

Status: DRAFT for owner approval. Nothing is built yet.

## Intent and why
Add a library of market algorithms that each score every company, a separate dashboard menu that
shows each algorithm's score and a one-line insight per stock and per sector, and a way for those
scores to feed the final long-term and short-term scores. The owner wants broad coverage ("all
kinds of algorithms", from whatever is available), shown now and counted in the final score later.
The underlying reason (what would make it a success) was not stated; the owner accepted the
cautious path below. This is research output, not investment advice.

## Goals
- 25+ deterministic algorithms across trend, momentum, volatility/risk, breakout, volume,
  relative strength, mean reversion, and (phase 2) value/quality.
- Every score is 0-100 with a raw value, a one-line reason and the price date it is as of. A
  missing input gives no score and a reason ("needs 200 days of prices"), never a guess.
- New "Algorithms" menu: per-stock table (all algorithms, long/short composite, agreement),
  per-sector heatmap, per-algorithm explanation page.
- Algorithm scores can move the final long/short scores through ONE capped blend weight.

## Non-goals
- No order execution, no buy/sell language (the wording guard stays: "positive lean",
  "negative lean", accumulate/hold/reduce/avoid/insufficient evidence).
- No machine-learning prediction, no parameter fitting, no backtest engine (a later project).
- No intraday data, no candlestick patterns or ADX/ATR (the price files carry close and
  volume only, no open/high/low).
- No change to the existing rule scores, signals bands or news/social display-only status.

## Anti-goals (failure even if it "works")
- A final label that moves because of an untested algorithm. Default blend weight is 0.
- Hidden effect: the company page must show final scores with and without algorithms.
- Scores that look precise but rest on thin history or one stale price file.

## Constraints
- Data files are the audit trail: scores are recomputed from price files and extracted Facts;
  each stored score references the price Source it was computed from.
- All thresholds and weights are PLACEHOLDER, uncalibrated, and listed in docs/PLACEHOLDERS.md.
- Runs on the 4 GB server (resize to 8 GB planned): pure Python/stdlib maths on lists of closes,
  no pandas/numpy additions needed, no model calls, so no Groq tokens.
- Kite prices are personal use only; no exchange or Moneycontrol scraping.

## Approaches considered
A. Plug-in library in code + new table + API + menu (CHOSEN, owner agreed): each algorithm a small
   tested function registered with id/family/horizon/description. Trade-off: more upfront work,
   but testable, explainable and replayable.
B. Algorithms defined in YAML from fixed templates: faster to add, but limited, harder to test.
C. Compute on the fly in the API: simplest, but nothing stored, so no history and no clean way
   to feed the scorecard.

## Design
**Module** `src/ier/algos/`: `registry.py` (Algorithm dataclass: id, name, family, horizon
long|short, min_history, inputs, description, compute(series, ctx) -> AlgoResult(score|None,
value|None, reason)), one file per family, `runner.py` (run all for one company), `composite.py`.
**Inputs**: the company's merged daily series (date, close, volume), the sector index series
from data/sources (nifty_*) for relative strength, peer momentum from our own scorecards, and
(phase 2) Facts.
**Algorithms, phase 1 (price only; thresholds are placeholders in config/algos.yaml):**
- Trend: sma50_vs_sma200 cross (long), price_vs_sma200 (long), ema12_vs_ema26 (short),
  macd_histogram (short), trend_slope_126d with R-squared (long).
- Momentum: mom_1m, mom_3m (short), mom_6m, mom_12m_skip_1m (long), rsi14 (short, mapped to a
  lean, extremes flagged), roc_20 (short).
- Volatility/risk: volatility_60d, max_drawdown_252d, downside_deviation_126d (all lower is
  better, long), sharpe_like_126d (long), bollinger_position_20 (short), zscore_50 (short).
- Breakout: high52_proximity (long), low52_distance (long), donchian20_close_breakout (short).
- Volume: obv_trend_50 (short), volume_ratio_20d confirmation (short).
- Relative strength: rs_vs_sector_index_3m (short), rs_vs_sector_index_12m (long) when the
  sector's index CSV exists; rs_rank_in_sector (percentile of mom_6m among seeded peers).
**Algorithms, phase 2 (need extracted Facts; score None with "needs X" until present):**
pe_ratio and earnings_yield (price / trailing-four-quarter EPS), peg, piotroski_f, altman_z,
magic_formula (earnings yield + return on capital), dupont_roe, graham_number. Needs new
extraction topics (total assets, current assets/liabilities, retained earnings, operating cash
flow, shares outstanding, book value per share). This costs extra Groq tokens per company, so
phase 2 is a separate plan the owner switches on per sector.
**Storage**: new table `algo_scores` (id, instrument_id, algo_id, as_of date, score float null,
value float null, reason text, source_id fk, computed_at, run_id), unique (instrument_id,
algo_id, as_of). Append-style like facts; alembic migration 0012.
**Composite**: per company, algo_long and algo_short = weighted mean of available long/short
algorithms (equal weights, placeholder); algo_confidence = share of algorithms available;
agreement = share of available algorithms on the same side of 50.
**Blend into final scores**: `config/algos.yaml`: `blend_weight: 0.0` (default), hard maximum
0.30 enforced in code, `enabled_families`. Final long = (1-w) * rules_long + w * algo_long, same
for short; if algo score is missing the rules score is used alone. Scorecard gains nullable
columns rules_long, rules_short, algo_long, algo_short, algo_weight, so the effect is always
visible. With w = 0 nothing changes in any existing score or signal.
**Pipeline**: runs inside the existing technicals node (same price series), so no new agent or
queue; failures leave the other scores untouched.
**API**: GET /algos (catalogue with descriptions), GET /stocks/{isin}/algos, GET /sectors/{macro}/algos,
GET /stocks/{isin}/algos/{algo_id}/series (chart data), GET /algos/{algo_id}/universe (all companies).
**Dashboard** (new "Algorithms" nav item; hand-built SVG like the existing charts, no new
dependency; one shared `PriceChart` component with overlay lines, a shaded band option, marker
dots and up to two sub-panels, plus a few small new chart components in `charts3.tsx`):
1. *Algorithms overview*: sector-by-family heatmap of composite scores; distribution histogram of
   composite scores per sector; "most positive / most negative lean" lists; agreement gauge
   (share of algorithms on the same side); data-coverage bar (how many algorithms had enough
   history).
2. *Stock algorithms page* (per company): a scoreboard (every algorithm, family, horizon,
   score bar, raw value, reason, as-of date); a radar of family scores; long vs short composite
   dials; and the algorithm charts below, grouped by family, each with a one-line "what it says
   now" caption and an "i" box with the algorithm's explanation and placeholder thresholds.
3. *Sector page*: ranking table of companies by composite, scatter of long vs short composite,
   family-by-company heatmap, relative-strength lines of each company against the sector index.
4. *Algorithm explainer page* (per algorithm): definition, formula in words, minimum history,
   thresholds, and a table of all companies' current values with a histogram.
5. *Company page* gets a compact "Rules score X / with algorithms Y (weight 0%, off)" card and
   a sparkline row of the top algorithm signals.
**Chart for each algorithm** (served by GET /stocks/{isin}/algos/{algo_id}/series, computed from
the price files so a chart is always reproducible; limited to 252 daily points by default):
- sma50_vs_sma200, price_vs_sma200, ema12_vs_ema26: price line with the two averages overlaid;
  cross points marked; shaded gap between them.
- macd_histogram: price line on top; MACD line, signal line and histogram bars below.
- trend_slope_126d: price (log scale option) with the fitted trend line and a band of one
  standard error; R-squared shown in the caption.
- mom_1m/3m/6m/12m_skip_1m, roc_20: rolling-return line with a zero line, plus a bar for each
  horizon (1m/3m/6m/12m) so the horizons compare at a glance.
- rsi14: RSI line in a 0-100 panel with 30/70 bands shaded.
- volatility_60d, downside_deviation_126d: rolling-volatility line with the company's own
  percentile band (25th-75th) shaded.
- max_drawdown_252d: underwater curve (drawdown from the running peak) with the worst trough
  marked.
- sharpe_like_126d: rolling risk-adjusted return line with a zero line.
- bollinger_position_20, zscore_50: price with Bollinger bands (or z-score line with +/-2 bands);
  current position marked.
- high52_proximity, low52_distance, donchian20_close_breakout: price with the 52-week high/low
  or 20-day channel drawn as stepped lines; breakouts marked.
- obv_trend_50, volume_ratio_20d: price on top, volume bars coloured by up/down day with the
  20-day average, and the OBV line below.
- rs_vs_sector_index_3m/12m: company and sector index rebased to 100 plus a relative-strength
  ratio line below.
- rs_rank_in_sector: horizontal bar of the company's momentum against every peer, the company
  highlighted.
- Phase 2 (value/quality): P/E and earnings-yield history line against the sector median;
  Piotroski as nine tick boxes; Altman Z as a gauge with safe/grey/distress zones; magic
  formula as a scatter of earnings yield vs return on capital across the sector; DuPont ROE as a
  stacked breakdown (margin, turnover, leverage); each shows "needs X" when facts are missing.
Every chart has a text alternative (a caption with the current value and meaning), keyboard
focus on its info box, colours that do not rely on red/green alone, and an "as of" date.
**Tests**: each algorithm against hand-computed series; missing/short history gives None;
composite and blend arithmetic; w = 0 leaves scorecards identical to today; max weight enforced;
API shapes; chart series match the algorithm's own computation; component tests for each
chart (renders, empty/short-history state, caption text); wording guard on every new string.

## Left to the builder
Exact score-mapping curves per algorithm (keep simple linear bands, documented as placeholders);
file layout inside src/ier/algos/; chart choices in the dashboard; naming of reasons.

## Build order (for the plan)
1. Registry, phase-1 algorithms, config, unit tests. 2. Table + migration + runner in the
technicals node. 3. Composite, blend, scorecard columns, w = 0 regression tests. 4. API.
5. Dashboard: shared PriceChart + charts3.tsx, the five pages above, nav item, company-page card. 6. Docs/PLACEHOLDERS. (Phase 2 = separate plan.)

## Open items for the owner
- When to switch the blend on: needs a backtest first; suggested starting weight 0.10 after it.
- Which sector index files you will export (the relative-strength algorithms need them).
