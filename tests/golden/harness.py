"""Golden-set harness: seed the synthetic IT fixture and run the REAL pipeline over it.

Real: Postgres, agent seeding, job queue + worker threads, company graph, fundamentals ingestion
(fetch + chunk + retrieve), rules engine with it.yaml, verifier, scorecard, signals.yaml.
Stubbed (so nothing touches the network or a model server):
  * the OpenAI client inside ier.llm.router (deterministic JSON built from it_golden.yaml),
  * httpx transport used by ier.tools.base.fetch (serves the fixture filings index and files;
    any other host fails like an unreachable one),
  * the embedder (tests.knowledge.conftest.fake_embed) instead of the sentence-transformers model.
The stub "model" only echoes what the YAML says it extracts or drafts, so a green run proves the
plumbing, not model quality. See it_golden.yaml header.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import yaml
from sqlalchemy import select

from ier.db.models import (Agent, AgentEvent, Chunk, Document, EvidenceLink, Fact, Instrument, Job,
                           Scorecard, Signal, Source)
from ier.graph import company as cg
from ier.knowledge import retrieval
from ier.knowledge.retrieval import ingest_document
from ier.runtime.events import seed_agents, tree
from ier.runtime.__main__ import run_until_drained
from ier.tools import base as tools_base
from ier.tools import exchange_filings
from tests.knowledge.conftest import fake_embed

GOLDEN_PATH = Path(__file__).with_name("it_golden.yaml")
FILINGS_HOST = "filings.invalid"
ORDER_WORDING = re.compile(
    r"\b(buy|sell|order|orders|stop[- ]?loss|target price|quantity|you should|we recommend)\b", re.I)
_EXCERPT = re.compile(r"<<<EXCERPT chunk_id=(\d+)>>>\n(.*?)\n<<<END EXCERPT \1>>>", re.S)


def load_golden() -> dict:
    with open(GOLDEN_PATH) as f:
        return yaml.safe_load(f)


# --- stub model ----------------------------------------------------------------------------

class StubModel:
    """Deterministic stand-in for the model server, driven by the golden YAML."""

    def __init__(self, golden: dict):
        self.golden = golden
        self.companies = golden["companies"]
        self.verdicts = {c["text"]: c.get("model_verdict", False)
                         for co in self.companies for c in co["claims"]}
        self.verdicts.update({i["text"]: i.get("model_verdict", False)
                              for i in golden["sector"]["drivers"] + golden["sector"]["risks"]})
        self.calls: list[str] = []

    def respond(self, prompt: str) -> dict:
        excerpts = {int(i): t for i, t in _EXCERPT.findall(prompt)}
        if "Extract reported financial figures" in prompt:
            self.calls.append("extract")
            co = self._company(excerpts)
            return {"facts": [{"metric": f["metric"], "value": f["value"], "unit": f["unit"],
                               "period": f["period"], "chunk_id": self._find(excerpts, f["find"])}
                              for f in co["facts"] if self._has(excerpts, f["find"])]}
        if "Write short, factual benefit and risk claims" in prompt:
            self.calls.append("synthesise")
            co = self._company(excerpts)
            return {"claims": [{"kind": c["kind"], "text": c["text"], "severity": c["severity"],
                                "horizon": c["horizon"],
                                "evidence_ids": [self._find(excerpts, s) for s in c["cite"]]}
                               for c in co["claims"]
                               if all(self._has(excerpts, s) for s in c["cite"])]}
        if "Decide whether the CLAIM is supported" in prompt:
            self.calls.append("verdict")
            claim = re.search(r"<<<CLAIM>>>\n(.*?)\n<<<END CLAIM>>>", prompt, re.S).group(1).strip()
            ok = self.verdicts.get(claim, False)  # unknown claim text: fail closed
            return {"supported": ok, "reason": "golden stub verdict"}
        if "List the main drivers and risks" in prompt:
            self.calls.append("sector")
            sec = self.golden["sector"]

            def item(i):
                return {"text": i["text"], "severity": i["severity"], "direction": i["direction"],
                        "evidence_ids": [self._find(excerpts, i["find"])]}
            return {"drivers": [item(i) for i in sec["drivers"]],
                    "risks": [item(i) for i in sec["risks"]]}
        raise AssertionError("golden stub model got a prompt it does not know:\n" + prompt[:300])

    def _company(self, excerpts: dict) -> dict:
        text = " ".join(excerpts.values())
        hits = [co for co in self.companies if co["name"] in text]
        assert len(hits) == 1, f"excerpts must belong to exactly one golden company, got {len(hits)}"
        return hits[0]

    @staticmethod
    def _has(excerpts: dict, needle: str) -> bool:
        return any(needle in t for t in excerpts.values())

    @staticmethod
    def _find(excerpts: dict, needle: str) -> int:
        for cid, t in excerpts.items():
            if needle in t:
                return cid
        raise AssertionError(f"fixture text {needle!r} is not in any retrieved excerpt")


class _FakeOpenAI:
    def __init__(self, stub: StubModel, **_kw):
        self._stub = stub
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **_kw):
        body = json.dumps(self._stub.respond(messages[0]["content"]))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))],
                               usage=None)


def install_stubs(mp, golden: dict, cache_dir: Path) -> dict:
    """Patch model, network and embedder on `mp` (a pytest MonkeyPatch). Returns recorders."""
    import openai
    stub = StubModel(golden)
    hosts: list[str] = []
    index = {co["isin"]: [
        {"url": f"https://{FILINGS_HOST}/golden/{co['key']}/{f['slug']}.txt",
         "filing_type": f["filing_type"], "period": f["period"], "published_date": f["published_date"]}
        for f in co["filings"]] for co in golden["companies"]}
    files = {f"/golden/{co['key']}/{f['slug']}.txt": f["text"]
             for co in golden["companies"] for f in co["filings"]}

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host != FILINGS_HOST:
            raise httpx.ConnectError("network is blocked in the golden run", request=request)
        path = request.url.path
        m = re.fullmatch(r"/index/(.+)\.json", path)
        if m and m.group(1) in index:
            return httpx.Response(200, content=json.dumps(index[m.group(1)]).encode())
        if path in files:
            return httpx.Response(200, content=files[path].encode())
        return httpx.Response(404)

    real_client = httpx.Client
    mp.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    mp.setattr(openai, "OpenAI", lambda **kw: _FakeOpenAI(stub, **kw))
    mp.setattr(retrieval, "_default_embedder", fake_embed)
    real_filings = exchange_filings.load_config   # shipped allowlist is empty (refuses all)
    mp.setattr(exchange_filings, "load_config",
               lambda *a, **k: {**real_filings(*a, **k), "allowed_hosts": {FILINGS_HOST: 1}})
    real_cfg = tools_base.load_config
    mp.setattr(tools_base, "load_config",
               lambda *a, **k: {**real_cfg(*a, **k), "min_interval_seconds": 0.0,
                                "cache_dir": str(cache_dir)})
    return {"stub": stub, "hosts": hosts}


# --- seeding and running -------------------------------------------------------------------

def _source(s, url, tier, doc_type):
    src = Source(url=url, type=doc_type, tier=tier, content_hash=f"golden-{abs(hash(url))}")
    s.add(src)
    s.flush()
    return src


def seed_world(factory, golden: dict) -> None:
    """Instruments, the agent tree, the Tier-2 sector document and each company's extra
    documents (rating note, forum chatter). Filings are NOT seeded: the pipeline fetches them."""
    sec = golden["sector"]
    with factory() as s:
        for co in golden["companies"]:
            s.add(Instrument(isin=co["isin"], nse_code=co["nse_code"], name=co["name"],
                             macro_sector=golden["macro_sector"], sector=sec["name"],
                             industry=sec["industry"], status="active",
                             listing_date=date(2020, 1, 1), avg_daily_value=5e7))
        s.flush()
        seed_agents(s, golden["macro_sector"])
        d = sec["document"]
        src = _source(s, d["url"], d["tier"], "sector_note")
        ingest_document(s, src.id, d["text"], {"sector": sec["name"], "title": d["title"],
                                               "doc_type": "sector_note"}, embed=fake_embed)
        for co in golden["companies"]:
            inst = s.scalars(select(Instrument).where(Instrument.isin == co["isin"])).one()
            for ex in co["extra_documents"]:
                src = _source(s, ex["url"], ex["tier"], ex["doc_type"])
                ingest_document(s, src.id, ex["text"],
                                {"instrument_id": inst.id, "title": ex["title"],
                                 "doc_type": ex["doc_type"], "period": ex["period"]},
                                embed=fake_embed)
        s.commit()


def run_pipeline(factory, macro_sector: str, run_id: str, *, handlers: dict, workers=None,
                 timeout: float = 300.0, config: dict | None = None) -> dict:
    from ier.graph.company import plan_run
    with factory() as s:
        plan_run(s, macro_sector, run_id)
        s.commit()
    return run_until_drained(factory, workers or {"sector": 1, "industry": 1, "company": 2},
                             handlers=handlers, timeout=timeout, config=config)


def make_handlers(mp) -> dict:
    """Default handlers incl. the real signal hook, without leaking the hook into other tests."""
    mp.setattr(cg, "SIGNAL_HOOK", None)
    return cg.register_default_handlers({})


# --- shared assertions (used by the golden tests and by the live test) ------------------------

def published_claims(session, scorecard: Scorecard) -> list[dict]:
    return [dict(c, kind=k) for k, items in (("benefit", scorecard.benefits), ("risk", scorecard.risks))
            for c in (items or [])]


def sourcing_problems(session, scorecard: Scorecard) -> list[str]:
    """Why a scorecard's published claims are not all backed by Tier 1/2 evidence (empty = ok)."""
    problems = []
    claims = published_claims(session, scorecard)
    links = session.scalars(select(EvidenceLink).where(EvidenceLink.scorecard_id == scorecard.id)).all()
    refs = {l.claim_ref for l in links}
    if len(refs) != len(claims):
        problems.append(f"{len(claims)} published claims but {len(refs)} claims have evidence links")
    for l in links:
        if l.chunk_id is None and l.fact_id is None:
            problems.append(f"link {l.id} cites neither a fact nor a chunk")
            continue
        src_id = (session.get(Chunk, l.chunk_id).source_id if l.chunk_id is not None
                  else session.get(Fact, l.fact_id).source_id)
        tier = session.get(Source, src_id).tier
        if tier not in (1, 2):
            problems.append(f"link {l.id} ({l.claim_ref}) rests on a tier {tier} source")
    return problems


def run_state_problems(session) -> list[str]:
    """Agents/jobs left in a state that a finished run must not leave (empty = ok)."""
    problems = []
    live = session.scalars(select(Job).where(Job.status.in_(("queued", "running")))).all()
    for j in live:
        problems.append(f"job {j.id} still {j.status}")

    def walk(node):
        if node.status in ("running", "queued") and not live:
            problems.append(f"agent {node.id} ({node.name}) shows {node.status} with no live job")
        for c in node.children:
            walk(c)
    walk(tree(session))
    latest: dict[int, str] = {}
    for ev in session.scalars(select(AgentEvent).where(
            AgentEvent.kind.in_(("queued", "started", "completed", "failed"))).order_by(AgentEvent.id)):
        latest[ev.agent_id] = ev.kind
    for aid, kind in latest.items():
        if kind in ("started", "queued"):
            problems.append(f"agent {aid} latest status event is a dangling {kind!r}")
    return problems
