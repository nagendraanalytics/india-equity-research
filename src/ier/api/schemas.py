"""Pydantic response models. Research labels only: no order, side, quantity or price fields."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

SIGNAL_LABEL = "research signal — not an order or investment advice"


class AgentNodeOut(BaseModel):
    id: int
    tier: int
    name: str
    status: str = Field(description=(
        "running | failed | degraded (a descendant failed) | blocked (queued, waiting for an "
        "unfinished parent job) | queued | completed | idle"))
    current_task: str | None
    last_run: datetime | None
    cost_today: float
    children: list["AgentNodeOut"]


class TreeOut(BaseModel):
    last_event_id: int   # resume the stream with Last-Event-ID: <this>
    tree: AgentNodeOut


class SummaryOut(BaseModel):
    queue_depth: int
    compute_tokens_today: int
    compute_model_seconds_today: float
    failures_today: int
    last_event_id: int


class MinutePoint(BaseModel):
    t: datetime
    started: int
    completed: int
    failed: int


class HourPoint(BaseModel):
    t: datetime
    tokens: int
    model_seconds: float


class RecentFailure(BaseModel):
    event_id: int
    agent_id: int
    created_at: datetime | None
    error: str | None


class MetricsOut(BaseModel):
    """Chart data for the fleet dashboard. Series are dense (zero-filled), oldest first."""
    events_by_minute: list[MinutePoint]   # last 60 minutes
    tokens_by_hour: list[HourPoint]       # last 24 hours
    jobs_by_status: dict[str, int]        # queued | running | done | failed
    recent_failures: list[RecentFailure]  # newest first, at most 10
    last_event_id: int


class AgentCost(BaseModel):
    cost_today: float
    event_cost_total: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model_seconds: float


class LastOutput(BaseModel):
    event_id: int
    created_at: datetime | None
    payload: Any


class LastError(BaseModel):
    event_id: int
    created_at: datetime | None
    message: str | None
    payload: Any


class AgentDetail(BaseModel):
    id: int
    tier: int
    tier_name: str
    role: str
    name: str
    parent_id: int | None
    scope_sector: str | None
    isin: str | None
    status: str
    current_task: str | None
    last_run: datetime | None
    rerunnable: bool
    cost: AgentCost
    last_output: LastOutput | None
    last_error: LastError | None


class EventOut(BaseModel):
    id: int
    agent_id: int
    kind: str
    payload: Any
    cost: float
    created_at: datetime | None


class EventsOut(BaseModel):
    events: list[EventOut]


class RerunOut(BaseModel):
    job_id: int
    agent_id: int
    already_queued: bool
    event_id: int | None


class Freshness(BaseModel):
    as_of: date | None
    age_days: int | None
    stale: bool


class FactOut(BaseModel):
    id: int
    metric: str
    value: float
    unit: str | None
    period: str
    as_of: date | None
    conflict: bool


class SourceOut(BaseModel):
    id: int
    url: str
    type: str
    tier: int
    publish_date: date | None
    fetched_at: datetime | None


class EvidenceOut(BaseModel):
    claim_type: str
    claim_ref: str | None
    fact: FactOut | None
    chunk_id: int | None
    source: SourceOut | None


class FindingsOut(BaseModel):
    scorecard_id: int
    long_term_score: float | None
    short_term_score: float | None
    confidence: str | None
    sub_scores: Any
    benefits: Any
    risks: Any
    red_flags: Any


class RuleInputs(BaseModel):
    rules_version: str | None
    scorecard_run_date: date | None
    scorecard_inputs: dict[str, Any]
    latest_run_inputs: Any


class RunHistoryItem(BaseModel):
    run_log_id: int
    run_id: str | None
    role: str | None
    model_version: str | None
    status: str | None
    created_at: datetime | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model_seconds: float


class AnalystOut(BaseModel):
    agent_id: int
    role: str
    name: str
    isin: str | None
    findings: FindingsOut | None
    confidence: str | None
    rules_version: str | None
    freshness: Freshness
    evidence: list[EvidenceOut]
    run_history: list[RunHistoryItem]
    rule_inputs: RuleInputs


class SignalOut(BaseModel):
    isin: str
    name: str
    sector: str | None
    industry: str | None
    rating: str
    reasons: list[str]
    confidence: str | None
    rules_version: str
    as_of: date
    age_days: int | None
    stale: bool
    label: str = SIGNAL_LABEL


class StockItem(BaseModel):
    isin: str
    name: str
    nse_code: str | None
    bse_code: str | None
    sector: str | None
    industry: str | None
    long_term_score: float | None
    short_term_score: float | None
    confidence: str | None
    rules_version: str | None
    as_of: date | None
    age_days: int | None
    stale: bool
    rating: str | None
    label: str = SIGNAL_LABEL


class StockDetail(StockItem):
    agent_id: int | None
    sub_scores: Any = None
    benefits: Any = None
    risks: Any = None
    red_flags: Any = None
    signal: SignalOut | None


class _ListMeta(BaseModel):
    count: int
    stale_threshold_days: int
    stale_threshold_status: str  # "PLACEHOLDER" until the threshold is reviewed


class HistoryPoint(BaseModel):
    run_date: date
    long_term_score: float | None
    short_term_score: float | None
    confidence: str | None
    rating: str | None


class HistoryOut(_ListMeta):
    isin: str
    items: list[HistoryPoint]
    label: str = SIGNAL_LABEL


class StocksOut(_ListMeta):
    items: list[StockItem]


class SignalsOut(_ListMeta):
    items: list[SignalOut]
    label: str = SIGNAL_LABEL


class SectorItem(BaseModel):
    sector: str
    industries: list[str]
    stock_count: int
    scored_count: int
    avg_long_term_score: float | None
    avg_short_term_score: float | None
    confidence: str | None            # weakest confidence among the sector's stocks
    confidence_counts: dict[str, int]
    signal_counts: dict[str, int]
    rules_version: str | None         # latest; all distinct versions in rules_versions
    rules_versions: list[str]
    as_of: date | None                # OLDEST latest-scorecard date among scored stocks
    age_days: int | None
    stale: bool
    label: str = SIGNAL_LABEL


class SectorsOut(_ListMeta):
    items: list[SectorItem]
    label: str = SIGNAL_LABEL


class SourceOut(BaseModel):
    id: str
    segment: str
    name: str
    kind: str
    terms: str | None = None
    refresh_minutes: int = 0
    status: str
    detail: str | None = None
    last_checked_at: datetime | None = None
    last_ok_at: datetime | None = None
    count: int | None = None
    latest: Any = None


class SegmentOut(BaseModel):
    id: str
    name: str
    note: str | None = None
    connected: int
    total: int
    sources: list[SourceOut]


class SourcesOut(BaseModel):
    segments: list[SegmentOut]
    connected: int
    total: int
    affects_score: bool
    note: str


# --- algorithm library -------------------------------------------------------------------

class AlgoInfo(BaseModel):
    id: str
    name: str
    family: str
    horizon: str
    min_history: int
    description: str
    band: list[float] | None = Field(None, description="PLACEHOLDER: value that scores 0, value that scores 100")
    has_chart: bool


class AlgoCatalogue(BaseModel):
    items: list[AlgoInfo]
    count: int
    blend_weight: float
    max_blend_weight: float
    notes: list[str] = []
    macro_sectors: list[str] = []


class AlgoCompositeOut(BaseModel):
    long: float | None
    short: float | None
    confidence: float
    agreement: float | None
    n_available: int
    n_total: int


class AlgoScoreOut(BaseModel):
    algo_id: str
    name: str
    family: str
    horizon: str
    score: float | None
    value: float | None
    reason: str
    as_of: date


class AlgoPair(BaseModel):
    long: float | None
    short: float | None


class AlgoBlendOut(AlgoPair):
    weight: float | None


class StockAlgosOut(BaseModel):
    isin: str
    as_of: date | None
    age_days: int | None
    stale: bool
    composite: AlgoCompositeOut | None
    rules: AlgoPair
    with_algorithms: AlgoBlendOut
    items: list[AlgoScoreOut]
    label: str = SIGNAL_LABEL


class SectorAlgoItem(BaseModel):
    isin: str
    name: str
    as_of: date | None
    composite: AlgoCompositeOut | None
    family_scores: dict[str, float | None]


class SectorAlgosOut(BaseModel):
    macro_sector: str
    items: list[SectorAlgoItem]
    count: int
    label: str = SIGNAL_LABEL


class UniverseItem(BaseModel):
    isin: str
    name: str
    macro_sector: str | None
    score: float | None
    value: float | None
    reason: str
    as_of: date


class UniverseOut(BaseModel):
    algo: AlgoInfo
    items: list[UniverseItem]
    count: int
    label: str = SIGNAL_LABEL


class ChartOut(BaseModel):
    dates: list[str]
    panels: list[dict[str, Any]]
    markers: list[dict[str, Any]]
    caption: str
    as_of: str | None = None
