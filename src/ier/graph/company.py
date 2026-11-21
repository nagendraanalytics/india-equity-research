"""Company agent graph, run planning and the default runtime handlers (spec 4.2, 4.2a).

One company run is a LangGraph StateGraph:

    fundamentals -> rules -> synthesis -> verifier -> scorecard -> signal

Responsible agent per node (events go to it): fundamentals -> the `fundamentals` worker;
synthesis, verifier, scorecard -> the `synthesis` worker; rules and signal -> the company
agent itself (deterministic, no model). Each worker emits started (on its first node),
one `step` per node boundary (cost = prompt + completion tokens of the RunLog rows written
during that node, 0 if none), `output`, then `completed`. The company agent emits
started / completed | failed for the whole run (only when `emit_lifecycle`; under the runtime
worker those three come from the worker itself, see `company_job_handler`).

Each node runs inside a savepoint. An exception rolls that node's DB changes back, emits
`failed` on the responsible agent (and on the company agent), stops the graph and the run
returns RunResult(status="failed"). BudgetExceeded is never caught: it propagates so the
runtime worker halts every worker (spec: compute caps stop all workers).

Unchanged inputs are skipped: before any agent runs, the filings index is listed (no model
call) and the run fingerprint (see `fingerprint`: listed filings, ingested sources, Fact set
marker, rules, calibration files/model names/scoring code version, rounded tailwind) is compared with the fingerprint of the state the last
completed run left behind (RunLog outputs.fingerprint_after). A failed listing never skips.

Sector tailwind: the sector handler stores its SectorView summary in an `output` AgentEvent
on the sector agent (payload kind "sector_view"); a company run reads the latest such event
of its sector agent (company -> industry -> sector via Agent.parent_id) and passes
tailwind_input to the rules engine as context. Research output, not advice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import event as sa_event
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ier.agents.feed_events import analyse_feed, view_payload
from ier.agents import fundamentals as _fund
from ier.agents.fundamentals import PHARMA_TOPICS, TOPICS, extract_facts
from ier.agents.technicals import extract_technicals
from ier.algos.config import config_sha, load_algo_config
from ier.algos.runner import run_algorithms
from ier.algos.series import load_index_bars, load_price_bars
from ier.algos.store import latest_composite, peer_momentum, save_results
from ier.agents.sector import analyse_sector, rank_industry
from ier.agents.synthesis import build_scorecard, draft_claims
from ier.agents.verifier import verify
from ier.db.models import Agent, AgentEvent, Document, Fact, Instrument, Job, RunLog, Source
from ier.llm.router import CAP_SCOPE_KEY, USAGE_TRACKERS_KEY, BudgetExceeded
from ier.llm.router import load_config as load_llm_config
from ier.rules.engine import evaluate, load_rules
from ier.runtime import worker as worker_mod
from ier.runtime.events import emit
from ier.runtime.queue import QUEUED, tier_number
from ier.tools.exchange_filings import list_filings
from ier.universe.filter import build_universe
from ier.tools.exchange_filings import load_config as load_filings_config
from ier.tools.feeds import NEWS, SOCIAL, feeds_signature

logger = logging.getLogger(__name__)

RULES_DIR = Path(__file__).resolve().parents[1] / "rules" / "config"
RULES_PATH = RULES_DIR / "it.yaml"            # default rule set, for an instrument with no macro sector
# NSE macro-economic sector -> (rule file, extraction topics). A macro sector that is not listed
# here ("Services", "Diversified") is refused rather than scored with another sector's rules.
SECTOR_SETUP = {
    "Information Technology": ("it.yaml", TOPICS),
    "Healthcare": ("pharma.yaml", PHARMA_TOPICS),
    "Financial Services": ("financials.yaml", _fund.FINANCIALS_TOPICS),
    "Fast Moving Consumer Goods": ("fmcg.yaml", _fund.FMCG_TOPICS),
    "Consumer Discretionary": ("consumer_discretionary.yaml", _fund.CONSUMER_DISCRETIONARY_TOPICS),
    "Energy": ("energy.yaml", _fund.ENERGY_TOPICS),
    "Commodities": ("commodities.yaml", _fund.COMMODITIES_TOPICS),
    "Industrials": ("industrials.yaml", _fund.INDUSTRIALS_TOPICS),
    "Telecommunication": ("telecom.yaml", _fund.TELECOM_TOPICS),
    "Utilities": ("utilities.yaml", _fund.UTILITIES_TOPICS),
}
RULES_BY_MACRO_SECTOR = {k: RULES_DIR / v[0] for k, v in SECTOR_SETUP.items()}


class UnsupportedSector(ValueError):
    """The instrument's macro sector has no rule set yet."""


def _setup_for(session: Session, instrument_id: int) -> tuple:
    macro = session.scalar(select(Instrument.macro_sector).where(Instrument.id == instrument_id))
    if not macro:
        return ("it.yaml", TOPICS)
    if macro not in SECTOR_SETUP:
        raise UnsupportedSector(
            f"no rule set for macro sector {macro!r}; supported: {', '.join(sorted(SECTOR_SETUP))}")
    return SECTOR_SETUP[macro]


def topics_for(session: Session, instrument_id: int) -> tuple:
    """Extraction topics for this instrument's macro sector."""
    return _setup_for(session, instrument_id)[1]


def rules_path_for(session: Session, instrument_id: int) -> Path:
    """The scoring rule file for this instrument's macro sector (IT rules when it has none)."""
    return RULES_DIR / _setup_for(session, instrument_id)[0]
RUN_TRIGGER = "run"
COMPANY_ROLE = "company"
WORKER_ROLES = ("fundamentals", "synthesis")
# Feed workers (price files, news, social): created on demand when a company has none yet, so
# databases seeded before these agents existed keep working without a re-seed.
FEED_ROLES = ("technicals", "news", "social")
NODES = ("fundamentals", "technicals", "news", "social", "rules", "synthesis", "verifier",
         "scorecard", "signal")
# node -> Agent.role of the agent that owns (and emits events for) it
NODE_AGENT = {"fundamentals": "fundamentals", "technicals": "technicals", "news": "news",
              "social": "social", "rules": COMPANY_ROLE, "synthesis": "synthesis",
              "verifier": "synthesis", "scorecard": "synthesis", "signal": COMPANY_ROLE}
FIRST_NODE = {"fundamentals": "fundamentals", "synthesis": "synthesis", "technicals": "technicals",
              "news": "news", "social": "social"}
LAST_NODE = {"fundamentals": "fundamentals", "synthesis": "scorecard", "technicals": "technicals",
             "news": "news", "social": "social"}
SECTOR_VIEW, INDUSTRY_RANKING = "sector_view", "industry_ranking"
MAX_ERROR_CHARS = 500
MODEL_ROLES = ("extract", "synthesise")  # model roles a company run uses

# Signal hook: callable(session, instrument_id, scorecard_id) -> None. None = not configured.
SIGNAL_HOOK: Callable[[Session, int, int], None] | None = None


@dataclass(frozen=True)
class RunResult:
    status: str  # "completed" | "skipped" | "failed"
    scorecard_id: int | None = None
    error: str | None = None


class CompanyRunFailed(RuntimeError):
    """Raised by company_job_handler so the runtime worker counts a failed attempt."""


# --- helpers -----------------------------------------------------------------------------

def _today():
    return datetime.now(timezone.utc).date()


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


class _Usage:
    """Sum prompt+completion tokens of RunLog rows for `run_id` written while active (router
    calls write one RunLog row per attempt): rows flushed in this session, and rows the router
    commits independently (record_usage tells registered trackers). Precise per node even when
    other companies of the same run write RunLog rows concurrently."""

    def __init__(self, session: Session, run_id: str):
        self.session, self.run_id, self.tokens = session, run_id, 0.0

    def add(self, instance) -> None:
        if isinstance(instance, RunLog) and instance.run_id == self.run_id:
            self.tokens += (instance.prompt_tokens or 0) + (instance.completion_tokens or 0)

    def _on(self, session, instance) -> None:
        self.add(instance)

    def __enter__(self):
        sa_event.listen(self.session, "pending_to_persistent", self._on)
        self.session.info.setdefault(USAGE_TRACKERS_KEY, []).append(self)
        return self

    def __exit__(self, *exc) -> None:
        sa_event.remove(self.session, "pending_to_persistent", self._on)
        trackers = self.session.info.get(USAGE_TRACKERS_KEY, [])
        if self in trackers:
            trackers.remove(self)


def _model_version() -> str:
    models = load_llm_config().get("models", {})
    return ";".join(f"{r}={models.get(r)}" for r in MODEL_ROLES)[:128]


def _sector_agent(session: Session, company: Agent) -> Agent | None:
    industry = session.get(Agent, company.parent_id) if company.parent_id else None
    sector = session.get(Agent, industry.parent_id) if industry and industry.parent_id else None
    return sector if sector is not None and sector.role == "sector" else None


def latest_sector_tailwind(session: Session, company_agent_id: int) -> float | None:
    """tailwind_input of the latest stored SectorView for this company's sector agent."""
    company = session.get(Agent, company_agent_id)
    sector = _sector_agent(session, company) if company is not None else None
    if sector is None:
        return None
    payload = session.scalars(
        select(AgentEvent.payload).where(
            AgentEvent.agent_id == sector.id, AgentEvent.kind == "output",
            AgentEvent.payload["kind"].as_string() == SECTOR_VIEW,
        ).order_by(AgentEvent.id.desc()).limit(1)
    ).first()
    return _finite(payload.get("tailwind_input")) if isinstance(payload, dict) else None


def filings_listing(session: Session, isin: str, *, client=None, config: dict | None = None,
                    fetch_config: dict | None = None) -> list[list] | None:
    """The filings index for `isin` as sorted [url, filing_type, period, published_date]
    rows, or None when the listing failed: list_filings raised, or the index fetch fell back
    to cached bytes (index Source marked stale). No model call. With no index_url configured
    there is nothing to discover: [] (not a failure)."""
    try:
        refs = list_filings(isin, session=session, client=client, config=config,
                            fetch_config=fetch_config)
    except Exception:  # noqa: BLE001 - unknown listing: never skip on it
        logger.warning("filings listing failed for %s; run will not be skipped", isin,
                    exc_info=True)
        return None
    cfg = {**load_filings_config(), **(config or {})}
    if cfg.get("index_url") and not cfg.get("local_dir"):
        index_url = cfg["index_url"].format(isin=isin)
        status = session.scalars(select(Source.status).where(Source.url == index_url)
                                 .order_by(Source.id.desc()).limit(1)).first()
        if status == "stale":
            return None
    return sorted([r.url, r.filing_type, r.period,
                   r.published_date.isoformat() if r.published_date else None] for r in refs)


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def calibration_marker() -> dict:
    """Everything besides data that changes a run's output: sha256 of signals.yaml and
    synthesis.yaml (verifier/synthesis thresholds), the configured model names, and the
    rules engine's SCORING_CODE_VERSION. Paths are read at call time (module attributes)."""
    from ier.agents import verifier
    from ier.rules import engine
    from ier.signals import compute
    return {
        "signals_sha256": _sha256_file(compute.DEFAULT_PATH),
        "synthesis_sha256": _sha256_file(verifier.CONFIG_PATH),
        "models": load_llm_config().get("models", {}),
        "scoring_code_version": engine.SCORING_CODE_VERSION,
    }


def filings_key(session: Session, instrument_id: int, listing: list | None) -> str | None:
    """Hash of what fundamentals extraction depends on: the listed filings, the content hash of
    every ingested filing source, and the calibration marker (models, scoring code version).
    Feed files (prices, news, social) are not part of it, so a new news file does not re-run
    the expensive extraction. None when the listing is unknown."""
    if listing is None:
        return None
    sources = session.execute(
        select(Source.id, Source.content_hash).join(Document, Document.source_id == Source.id)
        .where(Document.instrument_id == instrument_id).distinct().order_by(Source.id)).all()
    blob = json.dumps({"filings": listing, "sources": [[i, h] for i, h in sources],
                       "calibration": calibration_marker()}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def fingerprint(session: Session, instrument_id: int, listing: list | None,
                tailwind: float | None, rules_path: Path | None = None,
                feeds: list | None = None) -> str | None:
    """sha256 over the run's inputs, or None when the filings listing is unknown (never skip):
    the listed filings (url, type, period, published date); (source id, content hash) of
    every Source behind this instrument's Documents; the instrument's Fact set marker (count,
    max id); the rules version and the sha256 of the rules file; the calibration marker
    (signals.yaml, synthesis.yaml, model names, SCORING_CODE_VERSION, see calibration_marker);
    the tailwind input rounded to an integer (so model noise does not defeat the skip)."""
    if listing is None:
        return None
    rules_path = rules_path or RULES_PATH
    sources = session.execute(
        select(Source.id, Source.content_hash)
        .join(Document, Document.source_id == Source.id)
        .where(Document.instrument_id == instrument_id)
        .distinct().order_by(Source.id)
    ).all()
    n_facts, max_fact = session.execute(
        select(func.count(Fact.id), func.max(Fact.id)).where(Fact.instrument_id == instrument_id)
    ).one()
    raw = rules_path.read_bytes()
    blob = json.dumps({
        "filings": listing,
        "sources": [[sid, h] for sid, h in sources],
        "facts": [n_facts, max_fact],
        "rules_version": str(load_rules(rules_path)["version"]),
        "rules_sha256": hashlib.sha256(raw).hexdigest(),
        "calibration": calibration_marker(),
        "algos_sha256": config_sha(),
        "tailwind_input": None if tailwind is None else int(round(tailwind)),
        **({"feeds": feeds} if feeds else {}),
    }, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _last_completed_outputs(session: Session, agent_id: int) -> dict:
    out = session.scalars(select(RunLog.outputs).where(
        RunLog.agent_id == agent_id, RunLog.role == COMPANY_ROLE,
        RunLog.outputs["status"].as_string() == "completed").order_by(RunLog.id.desc()).limit(1)).first()
    return out if isinstance(out, dict) else {}


def _last_completed_fingerprint(session: Session, agent_id: int) -> str | None:
    """Fingerprint of the state the last completed run left behind (outputs.fingerprint_after:
    taken after its own ingestion and fact extraction, so a run's own new Facts and Sources do
    not defeat the next skip)."""
    outputs = session.scalars(
        select(RunLog.outputs).where(
            RunLog.agent_id == agent_id, RunLog.role == COMPANY_ROLE,
            RunLog.outputs["status"].as_string() == "completed",
        ).order_by(RunLog.id.desc()).limit(1)
    ).first()
    return outputs.get("fingerprint_after") if isinstance(outputs, dict) else None


# --- the graph ---------------------------------------------------------------------------

class _State(TypedDict, total=False):
    facts: int
    rule_result: Any
    claims: list
    verified: Any
    scorecard_id: int


@dataclass
class _Ctx:
    session: Session
    run_id: str
    company_id: int
    instrument_id: int
    agents: dict[str, int]  # role -> agent id (company + workers)
    tailwind: float | None
    extract_kwargs: dict = field(default_factory=dict)
    rules_path: Path = RULES_PATH
    isin: str = ""
    local_dir: str | None = None
    skip_extract: bool = False      # filings unchanged since the last completed run
    error: str | None = None
    failed_node: str | None = None
    started: set = field(default_factory=set)


def _node_fundamentals(ctx: _Ctx, state: _State):
    if ctx.skip_extract:   # same filings, same models: the facts already in the database stand
        return {"facts": 0}, {"facts": 0, "skipped": "filings unchanged"}
    kw = {k: v for k, v in ctx.extract_kwargs.items() if v is not None}
    facts = extract_facts(ctx.session, ctx.instrument_id, ctx.run_id, **kw)
    return {"facts": len(facts)}, {"facts": len(facts)}


def _run_algos(ctx: _Ctx, source_id: int | None) -> int:
    """Score the company with the algorithm library and store the results. Never fails the run:
    a problem here is logged and the company simply has no algorithm scores this time."""
    try:
        cfg = load_algo_config()
        bars, _ = load_price_bars(ctx.isin, ctx.local_dir)
        if not bars:
            return 0
        macro = ctx.session.scalar(select(Instrument.macro_sector).where(Instrument.id == ctx.instrument_id))
        index_id = cfg["sector_index"].get(macro) if macro else None
        with ctx.session.begin_nested():
            results = run_algorithms(
                bars, ctx.isin, cfg, today=_today(),
                index_bars=load_index_bars(ctx.session, index_id) if index_id else None,
                peer_momentum=peer_momentum(ctx.session, macro) if macro else None)
            return save_results(ctx.session, ctx.instrument_id, bars[-1].day, results, source_id, ctx.run_id)
    except Exception:  # noqa: BLE001
        logger.exception("algorithm scoring failed for %s", ctx.isin)
        return 0


def _node_technicals(ctx: _Ctx, state: _State):
    r = extract_technicals(ctx.session, ctx.instrument_id, ctx.isin, ctx.local_dir)
    n_algos = _run_algos(ctx, r.get("source_id"))
    return {"technicals": r}, {"files": r["files"], "as_of": r["as_of"], "saved": r["saved"],
                              "metrics": r["metrics"], "algorithms": n_algos}


def _feed_node(kind: str):
    def node(ctx: _Ctx, state: _State):
        view = analyse_feed(ctx.session, ctx.isin, kind, ctx.run_id, ctx.local_dir)
        payload = view_payload(view, ctx.run_id)
        emit(ctx.session, ctx.agents[kind], "output", payload)   # the company page reads this
        return {kind: payload}, {"files": len(view.files), "items": len(view.items), "dropped": view.dropped}
    return node


def _node_rules(ctx: _Ctx, state: _State):
    context = {"sector_tailwind": ctx.tailwind} if ctx.tailwind is not None else None
    rr = evaluate(ctx.instrument_id, ctx.rules_path, ctx.session, context=context)
    return {"rule_result": rr}, {"rules_version": rr.rules_version, "missing": list(rr.missing),
                                 "tailwind_input": ctx.tailwind}


def _node_synthesis(ctx: _Ctx, state: _State):
    claims = draft_claims(ctx.session, ctx.instrument_id, state["rule_result"], ctx.run_id)
    return {"claims": claims}, {"claims": len(claims)}


def _node_verifier(ctx: _Ctx, state: _State):
    verified = verify(state["claims"], ctx.session, instrument_id=ctx.instrument_id,
                      run_id=ctx.run_id)
    return {"verified": verified}, {"kept": len(verified.kept), "dropped": len(verified.dropped)}


def _node_scorecard(ctx: _Ctx, state: _State):
    cfg = load_algo_config()
    try:
        composite = latest_composite(ctx.session, ctx.instrument_id, cfg)
    except Exception:  # noqa: BLE001 - algorithms are optional; the rules score stands alone
        logger.exception("algorithm composite failed for %s", ctx.isin)
        composite = None
    card = build_scorecard(ctx.session, ctx.instrument_id, state["rule_result"],
                           state["verified"], _today(), algo=composite, algo_weight=cfg["blend_weight"])
    return {"scorecard_id": card.id}, {"scorecard_id": card.id}


def _node_signal(ctx: _Ctx, state: _State):
    hook = SIGNAL_HOOK  # read at call time so it can be configured after import
    if hook is None:
        return {}, {"message": "signal skipped: not configured"}
    hook(ctx.session, ctx.instrument_id, state["scorecard_id"])
    return {}, {"message": "signal hook called"}


_NODE_FUNCS = {"fundamentals": _node_fundamentals, "technicals": _node_technicals,
               "news": _feed_node(NEWS), "social": _feed_node(SOCIAL), "rules": _node_rules,
               "synthesis": _node_synthesis, "verifier": _node_verifier,
               "scorecard": _node_scorecard, "signal": _node_signal}


def _wrap(ctx: _Ctx, name: str):
    def run(state: _State) -> dict:
        s = ctx.session
        role = NODE_AGENT[name]
        agent_id = ctx.agents[role]
        base = {"run_id": ctx.run_id, "node": name}
        if FIRST_NODE.get(role) == name:
            emit(s, agent_id, "started", {"run_id": ctx.run_id, "task": name})
        try:
            with _Usage(s, ctx.run_id) as usage, s.begin_nested():
                update, detail = _NODE_FUNCS[name](ctx, state)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - isolated to this company run
            ctx.error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            ctx.failed_node = name
            emit(s, agent_id, "failed", {**base, "error": ctx.error})
            return {}
        emit(s, agent_id, "step", {**base, **detail}, cost=usage.tokens)
        if LAST_NODE.get(role) == name:
            out = {"run_id": ctx.run_id}
            if role == "fundamentals":
                out["facts"] = update["facts"]
            elif role in FEED_ROLES:
                out.update(detail)
            else:
                verified = {**state, **update}["verified"]
                out.update(claims=len(state.get("claims") or []), kept=len(verified.kept),
                           dropped=len(verified.dropped), scorecard_id=update["scorecard_id"])
            if role not in (NEWS, SOCIAL):   # those nodes emit their own `output` (the view)
                emit(s, agent_id, "output", out)
            emit(s, agent_id, "completed", {"run_id": ctx.run_id})
        return update
    return run


def build_graph(ctx: _Ctx):
    g = StateGraph(_State)
    for name in NODES:
        g.add_node(name, _wrap(ctx, name))
    g.add_edge(START, NODES[0])
    for here, nxt in zip(NODES, NODES[1:]):
        g.add_conditional_edges(here, lambda _s, nxt=nxt: END if ctx.error else nxt, [nxt, END])
    g.add_edge(NODES[-1], END)
    return g.compile()


# --- public entry points -----------------------------------------------------------------

def run_company_agent(session: Session, agent_id: int, run_id: str, *, force: bool = False,
                      emit_lifecycle: bool = True, client=None,
                      filings_config: dict | None = None,
                      fetch_config: dict | None = None) -> RunResult:
    """Run the company graph for one company agent. Flushes, never commits.

    Returns RunResult: "completed" (scorecard_id set), "skipped" (fingerprint equal to the
    state left by the last completed run and not `force`; no agent or model called) or
    "failed" (error set; the failing node's DB changes rolled back, earlier nodes' work kept).
    BudgetExceeded propagates. ValueError if `agent_id` is not a company agent with an
    instrument scope. Model usage during the run is attributed to this company agent, which is
    the scope of the per-run compute cap (session.info[CAP_SCOPE_KEY]). `client`, `filings_config` and `fetch_config` are passed to the filings
    listing and to extract_facts (test seams; None = configured defaults).
    """
    company = session.get(Agent, agent_id)
    if company is None or company.role != COMPANY_ROLE or company.scope_instrument_id is None:
        raise ValueError(f"agent {agent_id!r} is not a company agent with an instrument scope")
    previous_scope = session.info.get(CAP_SCOPE_KEY)
    session.info[CAP_SCOPE_KEY] = agent_id   # per-run compute cap = this company job
    try:
        return _run_company(session, company, run_id, force=force,
                            emit_lifecycle=emit_lifecycle, client=client,
                            filings_config=filings_config, fetch_config=fetch_config)
    finally:
        if previous_scope is None:
            session.info.pop(CAP_SCOPE_KEY, None)
        else:
            session.info[CAP_SCOPE_KEY] = previous_scope


def _run_company(session: Session, company: Agent, run_id: str, *, force: bool,
                 emit_lifecycle: bool, client, filings_config, fetch_config) -> RunResult:
    agent_id = company.id
    instrument_id = company.scope_instrument_id
    if emit_lifecycle:
        emit(session, agent_id, "started", {"run_id": run_id, "task": "company run"})
    log = RunLog(agent_id=agent_id, run_id=run_id, role=COMPANY_ROLE,
                 model_version=_model_version(),
                 inputs={"instrument_id": instrument_id, "agent_id": agent_id, "force": force})
    session.add(log)
    session.flush()

    def finish(result: RunResult, outputs: dict) -> RunResult:
        log.outputs = {"status": result.status, **outputs}
        session.flush()
        if emit_lifecycle:
            kind = "failed" if result.status == "failed" else "completed"
            payload = {"run_id": run_id, "status": result.status}
            if result.error:
                payload["error"] = result.error
            if result.scorecard_id is not None:
                payload["scorecard_id"] = result.scorecard_id
            emit(session, agent_id, kind, payload)
        return result

    workers = {a.role: a.id for a in session.scalars(
        select(Agent).where(Agent.parent_id == agent_id, Agent.role.in_(WORKER_ROLES))
        .order_by(Agent.id))}
    missing = [r for r in WORKER_ROLES if r not in workers]
    if missing:
        error = (f"company agent {agent_id} has no worker agent(s) for role(s) "
                 f"{', '.join(missing)}; run seed_agents")
        return finish(RunResult("failed", error=error), {"node": None, "error": error})

    for role in FEED_ROLES:   # feed workers are created on demand (older databases lack them)
        if role not in feed_workers(session, agent_id):
            session.add(Agent(tier=tier_number("worker"), parent_id=agent_id, role=role,
                              scope_instrument_id=instrument_id))
    session.flush()
    workers = {**workers, **feed_workers(session, agent_id)}

    isin = session.get(Instrument, instrument_id).isin
    local_dir = {**load_filings_config(), **(filings_config or {})}.get("local_dir") or None
    tailwind = latest_sector_tailwind(session, agent_id)
    listing = filings_listing(session, isin, client=client, config=filings_config,
                              fetch_config=fetch_config)
    feeds = feeds_signature(isin, local_dir)
    rules_path = rules_path_for(session, instrument_id)
    fp = fingerprint(session, instrument_id, listing, tailwind, rules_path, feeds=feeds)
    previous = _last_completed_fingerprint(session, agent_id)
    key_before = filings_key(session, instrument_id, listing)
    prev_key = _last_completed_outputs(session, agent_id).get("filings_key_after")
    skip_extract = bool(not force and key_before is not None and key_before == prev_key)
    log.inputs = {**log.inputs, "fingerprint": fp, "tailwind_input": tailwind,
                  "filings_listed": None if listing is None else len(listing),
                  "feed_files": len(feeds), "skip_extract": skip_extract}
    session.flush()

    if not force and fp is not None and previous == fp:
        emit(session, agent_id, "output", {"run_id": run_id, "skipped": "unchanged",
                                           "fingerprint": fp})
        return finish(RunResult("skipped"), {})

    ctx = _Ctx(session=session, run_id=run_id, company_id=agent_id, instrument_id=instrument_id,
               agents={COMPANY_ROLE: agent_id, **workers}, tailwind=tailwind,
               extract_kwargs=dict(client=client, config=filings_config,
                                   fetch_config=fetch_config,
                                   topics=topics_for(session, instrument_id)),
               rules_path=rules_path,
               isin=isin, local_dir=local_dir, skip_extract=skip_extract)
    final = build_graph(ctx).invoke({})
    if ctx.error:
        return finish(RunResult("failed", error=f"{ctx.failed_node}: {ctx.error}"),
                      {"node": ctx.failed_node, "error": ctx.error})
    scorecard_id = final.get("scorecard_id")
    after = fingerprint(session, instrument_id, listing, tailwind, rules_path, feeds=feeds)
    return finish(RunResult("completed", scorecard_id=scorecard_id),
                  {"scorecard_id": scorecard_id, "fingerprint_after": after,
                   "filings_key_after": filings_key(session, instrument_id, listing)})


def feed_workers(session: Session, company_id: int) -> dict[str, int]:
    return {a.role: a.id for a in session.scalars(select(Agent).where(
        Agent.parent_id == company_id, Agent.role.in_(FEED_ROLES)).order_by(Agent.id))}


def _children(session: Session, parent_id: int, role: str) -> list[Agent]:
    return list(session.scalars(select(Agent).where(
        Agent.parent_id == parent_id, Agent.role == role).order_by(Agent.id)))


def plan_run(session: Session, macro_sector: str, run_id: str) -> list[Job]:
    """Enqueue one run over the seeded agent tree of `macro_sector` (seed_agents first).

    One job per sector agent; per industry agent, depends_on its sector's job; per company
    agent, depends_on its industry's job (so after the sector job too). Payload {"run_id"}.
    Only companies whose instrument passes build_universe NOW are planned (an instrument that
    became suspended, surveillance-flagged or illiquid since seeding keeps its agent and
    history but gets no job).
    Idempotent per run_id: an agent that already has a job carrying this run_id (any status)
    gets no new one. Concurrency-safe: plan jobs are inserted ON CONFLICT DO NOTHING against
    the unique index uq_jobs_plan_run (migration 0010), so a concurrent plan_run for the same
    run_id waits for the other transaction and then reuses its jobs instead of duplicating.
    Returns the run's jobs (sector, then its industries, then companies).
    Flushes, never commits. ValueError if no macro_sector agent exists for the name.
    """
    macro = session.scalars(select(Agent).where(
        Agent.role == "macro_sector", Agent.scope_name == macro_sector,
        Agent.tier == tier_number("macro_sector")).order_by(Agent.id).limit(1)).first()
    if macro is None:
        raise ValueError(f"no macro_sector agent for {macro_sector!r}; run seed_agents first")
    existing: dict[int, Job] = {}
    for job in session.scalars(select(Job).where(
            Job.payload["run_id"].as_string() == run_id).order_by(Job.id)):
        existing.setdefault(job.agent_id, job)
    out: list[Job] = []
    eligible = {i.id for i in build_universe(session, macro_sector)}

    def ensure(agent: Agent, depends_on: int | None) -> Job:
        job = existing.get(agent.id)
        if job is None:
            job = _insert_plan_job(session, agent.id, run_id, depends_on)
            existing[agent.id] = job
        out.append(job)
        return job

    for sector in _children(session, macro.id, "sector"):
        sj = ensure(sector, None)
        for industry in _children(session, sector.id, "industry"):
            ij = ensure(industry, sj.id)
            for company in _children(session, industry.id, COMPANY_ROLE):
                if company.scope_instrument_id in eligible:
                    ensure(company, ij.id)
    return out


def _insert_plan_job(session: Session, agent_id: int, run_id: str, depends_on: int | None) -> Job:
    """INSERT the plan job ON CONFLICT DO NOTHING (uq_jobs_plan_run); emit `queued` only for a
    row this call created. On conflict, return the job the other transaction committed."""
    stmt = (pg_insert(Job).values(
        agent_id=agent_id, trigger=RUN_TRIGGER, status=QUEUED, attempts=0,
        scheduled_at=datetime.now(timezone.utc), payload={"run_id": run_id},
        depends_on=depends_on)
        .on_conflict_do_nothing(index_elements=[Job.agent_id, text("(payload->>'run_id')")],
                                index_where=text("trigger = 'run' AND payload->>'run_id' IS NOT NULL"))
        .returning(Job.id))
    new_id = session.execute(stmt).scalar_one_or_none()
    if new_id is not None:
        job = session.get(Job, new_id)
        emit(session, agent_id, "queued", {"job_id": new_id, "trigger": RUN_TRIGGER},
             own_transaction=False)   # durable only together with the Job row
        return job
    return session.scalars(select(Job).where(
        Job.agent_id == agent_id, Job.trigger == RUN_TRIGGER,
        Job.payload["run_id"].as_string() == run_id).order_by(Job.id).limit(1)).one()


# --- runtime handlers ----------------------------------------------------------------------

def _job_run_id(job: Job) -> str:
    """run_id from the job payload; manual jobs without one get a generated id."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    run_id = payload.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    return f"job-{job.id}-{uuid.uuid4().hex[:12]}"


def _item(i) -> dict:
    return {"text": i.text, "severity": i.severity, "direction": i.direction,
            "evidence_ids": list(i.evidence_ids)}


def run_sector_agent(session: Session, job: Job) -> None:
    """Handler for role "sector": analyse_sector for the agent's scope_name, then store the
    SectorView summary as an `output` event (kind "sector_view") that company runs read."""
    agent = session.get(Agent, job.agent_id)
    run_id = _job_run_id(job)
    with _Usage(session, run_id) as usage:
        view = analyse_sector(session, agent.scope_name, run_id)
    emit(session, agent.id, "step", {"run_id": run_id, "node": "analyse_sector"},
         cost=usage.tokens)
    emit(session, agent.id, "output", {
        "kind": SECTOR_VIEW, "run_id": run_id, "sector": view.sector,
        "tailwind_input": _finite(view.tailwind_input),
        "drivers": [_item(i) for i in view.drivers], "risks": [_item(i) for i in view.risks],
        "dropped": len(view.dropped),
    })


def run_industry_agent(session: Session, job: Job) -> None:
    """Handler for role "industry": rank_industry, stored as an `output` event."""
    agent = session.get(Agent, job.agent_id)
    run_id = _job_run_id(job)
    ranking = rank_industry(session, agent.scope_name)
    emit(session, agent.id, "step", {"run_id": run_id, "node": "rank_industry"})
    emit(session, agent.id, "output", {
        "kind": INDUSTRY_RANKING, "run_id": run_id, "industry": agent.scope_name,
        "ranking": [[iid, score] for iid, score in ranking],
    })


def company_job_handler(session: Session, job: Job) -> None:
    """Handler for role "company". The worker emits the company agent's started/completed/
    failed, so the run is called with emit_lifecycle=False. A failed run commits its events
    (and the work of the nodes that succeeded) and raises CompanyRunFailed, so the worker
    counts a failed attempt (retry with backoff) and emits `failed` on the company agent.
    Payload {"force": true} bypasses the unchanged-inputs skip."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    result = run_company_agent(session, job.agent_id, _job_run_id(job),
                               force=bool(payload.get("force")), emit_lifecycle=False)
    if result.status == "failed":
        session.commit()
        raise CompanyRunFailed(result.error)


def register_default_handlers(handlers: dict | None = None, *, overwrite: bool = True) -> dict:
    """Register the company, sector and industry handlers (keyed by Agent.role) in
    `handlers` (default: the runtime worker's HANDLERS registry). With overwrite=False a role
    that already has a handler keeps it. Returns the registry."""
    global SIGNAL_HOOK
    if SIGNAL_HOOK is None:  # never overwrite a caller's or test's hook
        from ier.signals.compute import signal_hook
        SIGNAL_HOOK = signal_hook
    reg = worker_mod.HANDLERS if handlers is None else handlers
    from ier.sources.handlers import run_segment_agent, run_source_agent
    for role, fn in ((COMPANY_ROLE, company_job_handler), ("sector", run_sector_agent),
                     ("industry", run_industry_agent), ("source", run_source_agent),
                     ("segment", run_segment_agent)):
        if overwrite or role not in reg:
            reg[role] = fn
    return reg
