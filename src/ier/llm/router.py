"""LLM router: every LLM call in the system goes through `complete`.

Open-source models only, served behind an OpenAI-compatible endpoint (Ollama or vLLM).
There is no per-token billing, so limits are compute caps (tokens and model-seconds) per
run and per UTC day, tracked in run_log and checked before each call.

Per-run scope: one job (a company job analyses one company's documents). The caller names the
job's agent in `session.info[CAP_SCOPE_KEY]` (the runtime worker and run_company_agent do);
usage rows carry that agent_id and the per_run cap sums rows with the same run_id AND agent_id,
so one plan run over many companies does not share one per-run budget. Without a scope the
per_run cap sums the whole run_id (legacy behaviour, e.g. a direct call in a script).

Usage rows are written in their own short transaction (like ier.runtime.events.emit) whenever
the session is bound to an Engine, so usage spent by a node or job that later rolls back still
counts against the caps. Residual overshoot: caps are checked before each call, so parallel
workers can each have one call in flight when a cap is reached; the overshoot is bounded by
(number of concurrent workers) x (one call's tokens/seconds).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import openai
import yaml
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ier.db.models import Agent, RunLog

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "models.yaml"
ROLES = ("extract", "synthesise", "reason")
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_OUTPUT_TOKENS = 2048
MAX_ATTEMPTS = 3  # first try + 2 retries on invalid JSON
MAX_RATE_WAITS = 8             # provider "try again in N s" waits per call before giving up
MAX_RATE_WAIT_SECONDS = 90.0   # a longer requested wait is not honoured (the job fails/retries)

Role = Literal["extract", "synthesise", "reason"]
CAP_SCOPE_KEY = "cap_scope_agent_id"   # session.info key: agent id of the job being run
USAGE_TRACKERS_KEY = "usage_trackers"  # session.info key: objects with .add(RunLog)


class BudgetExceeded(Exception):
    """A run or daily compute cap has been reached; no model call was made.

    `scope` is "per_run" (this job's own budget: the job fails, others go on) or "per_day"
    (everything stops until the next UTC midnight). Default "per_day" (the safe reading)."""

    def __init__(self, message: str = "", scope: str = "per_day"):
        super().__init__(message)
        self.scope = scope


class SchemaError(Exception):
    """The model did not return JSON matching the schema after all attempts."""


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _retry_after(exc: Exception) -> float | None:
    """Seconds the provider asks us to wait after a 429 (Retry-After header, else the
    "try again in 14.4s" / "...in 250ms" text), plus a 1 s margin; None if not stated."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        value = headers.get("retry-after")
        if value is not None:
            return float(value) + 1.0
    except (TypeError, ValueError):
        pass
    m = re.search(r"try again in\s+([\d.]+)\s*(ms|s)\b", str(exc), re.IGNORECASE)
    if m:
        secs = float(m.group(1)) / (1000.0 if m.group(2).lower() == "ms" else 1.0)
        return secs + 1.0
    return None


def _extract_json(content: str) -> str:
    """Strip a markdown code fence, else take the span from the first { to the last }."""
    m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        return content[start : end + 1]
    return content


def _used(session: Session, *filters) -> tuple[int, float]:
    tokens, seconds = session.execute(
        select(
            func.coalesce(func.sum(RunLog.prompt_tokens + RunLog.completion_tokens), 0),
            func.coalesce(func.sum(RunLog.model_seconds), 0.0),
        ).where(*filters)
    ).one()
    return int(tokens), float(seconds)


def _check_caps(session: Session, run_id: str, caps: dict) -> None:
    """Raise BudgetExceeded (with its scope) if this job's run budget or today's is used up.
    The per-day cap is checked first: when both are reached, everything must stop."""
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    scope_agent = session.info.get(CAP_SCOPE_KEY)
    per_run = [RunLog.run_id == run_id]
    if scope_agent is not None:
        per_run.append(RunLog.agent_id == scope_agent)
    scopes = {
        "per_day": _used(session, RunLog.created_at >= midnight),
        "per_run": _used(session, *per_run),
    }
    for scope, (tokens, seconds) in scopes.items():
        cap = caps[scope]
        if tokens >= cap["max_tokens"]:
            raise BudgetExceeded(
                f"{scope} max_tokens cap reached ({tokens}/{cap['max_tokens']})", scope=scope)
        if seconds >= cap["max_model_seconds"]:
            raise BudgetExceeded(
                f"{scope} max_model_seconds cap reached ({seconds:.1f}/{cap['max_model_seconds']})",
                scope=scope)


def record_usage(session: Session, row: RunLog) -> RunLog:
    """Persist one usage row so it survives a rollback of the caller's work.

    Sets row.agent_id from session.info[CAP_SCOPE_KEY] when unset. If the session is bound to
    an Engine (and the agent row is committed), the row commits in its own short transaction on
    a separate connection; otherwise (session bound to a Connection, e.g. tests wrapped in one
    outer transaction) it joins the caller's transaction, as events.emit does. Usage trackers
    registered in session.info[USAGE_TRACKERS_KEY] are told about an independently written row
    (rows added to the session itself reach them through their own session listener)."""
    if row.agent_id is None:
        row.agent_id = session.info.get(CAP_SCOPE_KEY)
    bind = session.get_bind()
    if isinstance(bind, Engine):
        with Session(bind, expire_on_commit=False) as own:
            if row.agent_id is None or own.get(Agent, row.agent_id) is not None:
                own.add(row)
                own.commit()
                for tracker in list(session.info.get(USAGE_TRACKERS_KEY, ())):
                    tracker.add(row)
                return row
    session.add(row)
    session.flush()
    return row


def complete(
    role: Role,
    prompt: str,
    schema: type[BaseModel],
    run_id: str,
    *,
    session: Session,
    client=None,
    clock=time.monotonic,
    config: dict | None = None,
    sleep=time.sleep,
) -> BaseModel:
    """Run `prompt` on the model for `role` and return a validated `schema` instance.

    Caps are checked before every attempt. Each attempt (including invalid-JSON ones) is
    logged as a RunLog row via record_usage (independent transaction when possible, so usage
    is never lost to a rollback). Otherwise the caller owns the transaction.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
    cfg = config or load_config()
    model = cfg["models"][role]
    if client is None:
        client = openai.OpenAI(
            base_url=cfg["base_url"],
            # hosted OpenAI-compatible providers need a key; local Ollama/vLLM ignore it
            api_key=os.environ.get("IER_LLM_API_KEY") or "not-needed",
            timeout=float(cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            max_retries=0,
        )
    max_output = int(cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))

    full_prompt = (
        f"{prompt}\n\nRespond with a single JSON object matching this JSON schema, "
        f"and nothing else:\n{json.dumps(schema.model_json_schema())}"
    )
    last_error: str | None = None
    for _ in range(MAX_ATTEMPTS):
        _check_caps(session, run_id, cfg["caps"])
        sent = full_prompt
        if last_error:
            sent += f"\n\nYour previous answer was invalid: {last_error}\nReturn corrected JSON only."
        try:
            waits = 0
            while True:
                start = clock()  # a 429 wait is not model time: the clock restarts per request
                try:
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": sent}],
                        response_format={"type": "json_object"},
                        max_tokens=max_output,
                    )
                    break
                except openai.RateLimitError as exc:
                    delay = _retry_after(exc)
                    if delay is None or delay > MAX_RATE_WAIT_SECONDS or waits >= MAX_RATE_WAITS:
                        raise
                    waits += 1
                    sleep(delay)  # provider throttled us (no tokens spent): wait, then retry
        except Exception:
            record_usage(
                session,
                RunLog(
                    run_id=run_id,
                    role=role,
                    model_version=model,
                    prompts=prompt,
                    outputs=None,
                    prompt_tokens=_estimate_tokens(sent),
                    completion_tokens=0,
                    model_seconds=clock() - start,
                ),
            )
            raise
        elapsed = clock() - start
        content = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if prompt_tokens is None or completion_tokens is None:
            prompt_tokens = _estimate_tokens(sent)
            completion_tokens = _estimate_tokens(content)
        parsed = None
        try:
            parsed = schema.model_validate_json(_extract_json(content))
        except ValidationError as exc:
            last_error = str(exc)
        record_usage(
            session,
            RunLog(
                run_id=run_id,
                role=role,
                model_version=model,
                prompts=prompt,
                outputs=parsed.model_dump(mode="json") if parsed else None,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                model_seconds=elapsed,
            ),
        )
        if parsed is not None:
            return parsed
    raise SchemaError(f"no valid {schema.__name__} JSON after {MAX_ATTEMPTS} attempts")
