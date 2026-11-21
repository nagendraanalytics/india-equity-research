"""`python -m ier.runtime`: the operator's launcher for the autonomous runtime.

    python -m ier.runtime load-classification path/to/classification.csv
    python -m ier.runtime seed "Information Technology"
    python -m ier.runtime plan "Information Technology" [--run-id ID]
    python -m ier.runtime workers [--sector N] [--industry N] [--company N] [--drain] [--timeout S]
    python -m ier.runtime watch [--interval S] [--sector N] [--industry N] [--company N]
    python -m ier.runtime purge-tier3 [--apply]

`workers` runs until Ctrl-C (SIGINT/SIGTERM), or with --drain until no runnable job is left.
It registers the company, sector and industry handlers (ier.graph.company) and, when a company
run finishes, the signal hook. Database: IER_DATABASE_URL (see ier.db.session).
Research labels only: nothing here places an order.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from ier.db import session as db_session
from ier.db.models import Job
from ier.runtime import queue as queue_mod
from ier.runtime.worker import run_workers

POLL_SECONDS = 0.2


def _active(session: Session) -> tuple[int, int]:
    """(running jobs, queued jobs a worker could claim right now: due, parent done).

    ONE statement, so one snapshot: two reads could straddle a claim or a completion and
    report "nothing left" while a job is in flight.
    """
    parent = aliased(Job)
    parent_done = select(parent.id).where(parent.id == Job.depends_on,
                                          parent.status.in_(queue_mod.SETTLED))
    now = datetime.now(timezone.utc)
    running = select(func.count(Job.id)).where(Job.status == queue_mod.RUNNING).scalar_subquery()
    runnable = select(func.count(Job.id)).where(
        Job.status == queue_mod.QUEUED,
        or_(Job.scheduled_at.is_(None), Job.scheduled_at <= now),
        or_(Job.depends_on.is_(None), parent_done.exists())).scalar_subquery()
    r, q = session.execute(select(running, runnable)).one()
    return int(r), int(q)


def _counts(session: Session) -> dict[str, int]:
    """Jobs by status, plus `backoff`: queued jobs not yet due (retry backoff, or a per-day
    compute cap rescheduled to the next UTC midnight); they are part of `queued`."""
    rows = dict(session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all())
    out = {s: int(rows.get(s, 0)) for s in (queue_mod.QUEUED, queue_mod.RUNNING,
                                            queue_mod.DONE, queue_mod.FAILED)}
    out["backoff"] = int(session.scalar(select(func.count(Job.id)).where(
        Job.status == queue_mod.QUEUED,
        Job.scheduled_at > datetime.now(timezone.utc))) or 0)
    return out


def run_until_drained(session_factory: Callable[[], Session], tiers: dict[str, int], *,
                      handlers: dict | None = None, timeout: float = 3600.0,
                      config: dict | None = None, grace: float = 10.0,
                      stop: threading.Event | None = None) -> dict:
    """Run workers until no job is running and none is runnable, then stop them.

    A job whose parent failed is runnable (the parent's output is optional). Jobs waiting on a
    retry backoff (or a per-day cap reschedule) still in the future are not runnable: they stay
    queued and are reported as `backoff`. Returns the job counts by status (plus `backoff`)
    and `timed_out` (True if `timeout` seconds passed first, or the workers halted early on a
    compute cap with runnable jobs left). Workers are stopped (they finish an in-flight job
    first, waiting at most `grace` seconds).
    """
    stop = stop or threading.Event()
    halt = threading.Event()
    thread = threading.Thread(
        target=run_workers, args=(tiers, stop), daemon=True, name="ier-run-workers",
        kwargs=dict(session_factory=session_factory, handlers=handlers, halt=halt,
                    config=config, poll_interval=POLL_SECONDS))
    thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while True:
            with session_factory() as s:
                running, runnable = _active(s)
            if not running and not runnable:
                break
            if halt.is_set() or stop.is_set():
                timed_out = halt.is_set()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(POLL_SECONDS)
    finally:
        stop.set()
        thread.join(grace)
    with session_factory() as s:
        counts = _counts(s)
    return {**counts, "timed_out": timed_out}


def _tiers_from_args(args) -> dict[str, int]:
    cfg = dict(queue_mod.load_config().get("workers", {}))
    for name in ("sector", "industry", "company"):
        n = getattr(args, name)
        if n is not None:
            cfg[name] = n
    return {k: int(v) for k, v in cfg.items() if int(v) > 0}


def _kite_prices(args, factory, *, client=None, now_fn=None) -> int:
    """`kite-prices`: fetch daily candles once, or every --loop minutes. Read-only; prints one
    line per company. A rejected or expired session stops the run with a clear message."""
    from ier.tools import exchange_filings, zerodha
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    cfg = zerodha.load_config()
    local_dir = exchange_filings.load_config().get("local_dir")
    if not local_dir:
        sys.exit("error: set local_dir in src/ier/tools/config/exchange_filings.yaml first")
    symbols = [s for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    if args.loop is not None and args.loop < 5:
        sys.exit("error: --loop must be at least 5 minutes")
    stop = threading.Event()
    if client is None:
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    while True:
        try:
            if client is None:
                key, token = zerodha.load_token(zerodha.token_path(cfg), now_fn())
                use = zerodha.KiteClient(key, token, min_gap=float(cfg["min_gap_seconds"]))
            else:
                use = client
            with factory() as s:
                results = zerodha.fetch_prices(s, use, cfg, local_dir, now=now_fn(), symbols=symbols,
                                               include_today=args.intraday, days=args.days)
        except zerodha.KiteError as exc:
            print(f"error: {exc}")
            return 1
        for r in results:
            tail = r["error"] or f"{r['rows']} rows, as of {r['asof']}"
            print(f"{r['symbol']:<9} {r['isin'] or '-':<13} {r['status']:<9} {tail}")
        if args.loop is None or stop.is_set():
            return 1 if any(r["status"] == "error" for r in results) else 0
        print(f"next refresh in {args.loop:g} min; Ctrl-C to stop")
        stop.wait(args.loop * 60)
        if stop.is_set():
            return 0


def _nse_prices(args, factory, *, client=None, now_fn=None) -> int:
    """`nse-prices`: bring every company's daily price file up to date from NSE's public end-of-day files,
    once or every --loop minutes. Read-only; prints one line per company."""
    from ier.tools import exchange_filings, nse_prices
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    cfg = nse_prices.load_config()
    local_dir = exchange_filings.load_config().get("local_dir")
    if not local_dir:
        sys.exit("error: set local_dir in src/ier/tools/config/exchange_filings.yaml first")
    Path(local_dir).expanduser().mkdir(parents=True, exist_ok=True)
    symbols = [s for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    if args.loop is not None and args.loop < 5:
        sys.exit("error: --loop must be at least 5 minutes")
    stop = threading.Event()
    if client is None:
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    while True:
        try:
            use = client or nse_prices.NseClient(min_gap=float(cfg["min_gap_seconds"]), user_agent=cfg["user_agent"],
                                                 timeout=float(cfg["timeout_seconds"]))
            with factory() as s:
                results = nse_prices.update_prices(s, use, cfg, local_dir, now=now_fn(), symbols=symbols, days=args.days)
        except nse_prices.NseError as exc:
            print(f"error: {exc}")
            return 1
        for r in results:
            tail = r["error"] or f"{r['rows']} rows, as of {r['asof']}"
            print(f"{r['symbol']:<12} {r['isin'] or '-':<13} {r['status']:<9} {tail}")
        if args.loop is None or stop.is_set():
            return 1 if any(r["status"] == "error" for r in results) else 0
        print(f"next refresh in {args.loop:g} min; Ctrl-C to stop")
        stop.wait(args.loop * 60)
        if stop.is_set():
            return 0


def _nse_filings(args, factory, *, client=None, now_fn=None) -> int:
    """`nse-filings`: download missing quarterly results and annual reports from NSE into each company's
    folder, once or every --loop minutes. Read-only; prints one line per company and kind."""
    from ier.tools import exchange_filings, nse_filings
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    cfg = nse_filings.load_config()
    local_dir = exchange_filings.load_config().get("local_dir")
    if not local_dir:
        sys.exit("error: set local_dir in src/ier/tools/config/exchange_filings.yaml first")
    Path(local_dir).expanduser().mkdir(parents=True, exist_ok=True)
    symbols = [s for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    kinds = ("results", "annual") if args.kind == "all" else (args.kind,)
    if args.loop is not None and args.loop < 5:
        sys.exit("error: --loop must be at least 5 minutes")
    stop = threading.Event()
    if client is None:
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    while True:
        try:
            use = client or nse_filings.NseFilingsClient(cfg)
            with factory() as s:
                results = nse_filings.update_filings(s, use, cfg, local_dir, now=now_fn(), symbols=symbols, kinds=kinds)
        except nse_filings.NseError as exc:
            print(f"error: {exc}")
            return 1
        for r in results:
            tail = r["error"] or f"{r['files']} new file(s)"
            print(f"{r['symbol']:<12} {r['isin'] or '-':<13} {r['kind']:<8} {r['status']:<9} {tail}")
        bad = any(r["status"] in ("blocked", "error") for r in results)
        if args.loop is None or stop.is_set():
            return 1 if bad else 0
        print(f"next refresh in {args.loop:g} min; Ctrl-C to stop")
        stop.wait(args.loop * 60)
        if stop.is_set():
            return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ier.runtime", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    lc = sub.add_parser("load-classification", help="load the NSE/BSE classification CSV")
    lc.add_argument("csv", type=Path)
    sd = sub.add_parser("seed", help="create the agent tree for one macro sector")
    sd.add_argument("macro_sector")
    pl = sub.add_parser("plan", help="enqueue one run over the seeded agent tree")
    pl.add_argument("macro_sector")
    pl.add_argument("--run-id", default=None)
    wk = sub.add_parser("workers", help="start the workers")
    for name in ("sector", "industry", "company"):
        wk.add_argument(f"--{name}", type=int, default=None, metavar="N",
                        help=f"worker threads for the {name} tier (default: config/runtime.yaml)")
    wk.add_argument("--drain", action="store_true", help="exit when no runnable job is left")
    wk.add_argument("--timeout", type=float, default=3600.0, help="--drain: give up after S seconds")
    wa = sub.add_parser("watch", help="run workers and queue a company run when its filings folder changes")
    for name in ("sector", "industry", "company"):
        wa.add_argument(f"--{name}", type=int, default=None, metavar="N")
    wa.add_argument("--interval", type=float, default=15.0, help="seconds between folder scans")
    sub.add_parser("kite-login", help="log in to Zerodha (read-only price data); needed once a day")
    kp = sub.add_parser("kite-prices", help="download daily prices from Zerodha into each company's folder")
    kp.add_argument("--symbols", default=None, help="comma-separated NSE symbols (default: config/zerodha.yaml)")
    kp.add_argument("--days", type=int, default=None, help="calendar days of history (default: config)")
    kp.add_argument("--intraday", action="store_true",
                    help="include today's unfinished candle (every change re-runs the company: costs model tokens)")
    kp.add_argument("--loop", type=float, default=None, metavar="MINUTES",
                    help="repeat every MINUTES (at least 5) until Ctrl-C")
    npx = sub.add_parser("nse-prices", help="download daily prices from NSE's public end-of-day files into each company's folder")
    npx.add_argument("--symbols", default=None, help="comma-separated NSE symbols (default: every company with an NSE code)")
    npx.add_argument("--days", type=int, default=None, help="calendar days of history for a company with none (default: config)")
    npx.add_argument("--loop", type=float, default=None, metavar="MINUTES", help="repeat every MINUTES (at least 5) until Ctrl-C")
    nfl = sub.add_parser("nse-filings", help="download quarterly results and annual reports from NSE into each company's folder")
    nfl.add_argument("--symbols", default=None, help="comma-separated NSE symbols (default: every company with an NSE code)")
    nfl.add_argument("--kind", choices=("results", "annual", "all"), default="all", help="which filings to fetch (default: all)")
    nfl.add_argument("--loop", type=float, default=None, metavar="MINUTES", help="repeat every MINUTES (at least 5) until Ctrl-C")
    pt = sub.add_parser("purge-tier3", help="delete Facts from Tier 3 sources (dry run unless --apply)")
    pt.add_argument("--apply", action="store_true", help="really delete (default: dry run)")
    return p


def main(argv: list[str] | None = None, *, session_factory: Callable[[], Session] | None = None,
         handlers: dict | None = None) -> int:
    args = _parser().parse_args(argv)
    factory = session_factory or db_session.get_session
    if args.command == "load-classification":
        from ier.taxonomy.loader import load_classification
        with factory() as s:
            n = load_classification(args.csv, s)
            s.commit()
        print(f"loaded {n} instruments from {args.csv}")
    elif args.command == "seed":
        from ier.runtime.events import seed_agents
        with factory() as s:
            created = seed_agents(s, args.macro_sector)
            from ier.sources.service import seed_source_agents
            created += seed_source_agents(s)
            s.commit()
        print(f"seeded {created} new agents for {args.macro_sector!r}")
    elif args.command == "plan":
        from ier.graph.company import plan_run
        run_id = args.run_id or f"run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        with factory() as s:
            try:
                jobs = plan_run(s, args.macro_sector, run_id)
            except ValueError as exc:
                sys.exit(f"error: {exc}")
            s.commit()
        print(f"run {run_id}: {len(jobs)} jobs queued for {args.macro_sector!r}")
    elif args.command == "purge-tier3":
        from ier.agents.fundamentals import purge_tier3_facts
        with factory() as s:
            r = purge_tier3_facts(s, apply=args.apply)
            s.commit()
        if args.apply:
            print(f"deleted {r['deleted']} of {r['found']} Tier 3 facts "
                  f"({r['kept_cited']} kept: cited by evidence links)")
        else:
            print(f"dry run: {r['found']} Tier 3 facts found ({r['kept_cited']} cited by evidence "
                  "links, would be kept); re-run with --apply to delete")
    elif args.command == "kite-login":
        from ier.tools import zerodha
        return zerodha.login_flow(os.environ, zerodha.load_config())
    elif args.command == "kite-prices":
        return _kite_prices(args, factory)
    elif args.command == "nse-prices":
        return _nse_prices(args, factory)
    elif args.command == "nse-filings":
        return _nse_filings(args, factory)
    elif args.command == "watch":
        from ier.runtime.watch import watch_loop
        from ier.tools import exchange_filings
        local_dir = exchange_filings.load_config().get("local_dir")
        if not local_dir:
            sys.exit("error: set local_dir in src/ier/tools/config/exchange_filings.yaml first")
        tiers = _tiers_from_args(args)
        if handlers is None:
            from ier.graph.company import register_default_handlers
            from ier.runtime.worker import HANDLERS
            handlers = register_default_handlers(HANDLERS, overwrite=False)
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        from ier.sources.service import schedule_due
        threading.Thread(target=watch_loop, args=(factory, local_dir, stop),
                         kwargs={"interval": args.interval, "scheduler": schedule_due}, daemon=True).start()
        print(f"watching {local_dir} every {args.interval:g}s; workers {tiers}; Ctrl-C to stop")
        run_workers(tiers, stop, session_factory=factory, handlers=handlers)
    else:
        tiers = _tiers_from_args(args)
        if handlers is None:
            from ier.graph.company import register_default_handlers
            from ier.runtime.worker import HANDLERS
            handlers = register_default_handlers(HANDLERS, overwrite=False)
        if args.drain:
            summary = run_until_drained(factory, tiers, handlers=handlers, timeout=args.timeout)
            print("drained: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
            # non-zero whenever work is left or failed: timed out, failed jobs, or jobs still
            # queued (e.g. waiting in retry backoff: run --drain again later)
            return 1 if summary["timed_out"] or summary["failed"] or summary["queued"] else 0
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        print(f"workers started: {tiers}; Ctrl-C to stop")
        run_workers(tiers, stop, session_factory=factory, handlers=handlers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
