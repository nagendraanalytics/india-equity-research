"""Technicals / news / social nodes inside the company run, and the filings-extraction skip."""
import csv
from datetime import date, timedelta

from sqlalchemy import select

from ier.agents import feed_events
from ier.agents.feed_events import FeedItem, FeedItems
from ier.db.models import Agent, AgentEvent, Fact
from ier.graph import company as cg
from tests.graph.test_company import _child, _company, _events, stubs, world  # noqa: F401


def _prices(path, n=120):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Close", "Volume"])
        for i in range(n):
            w.writerow([(date(2026, 1, 1) + timedelta(days=i)).isoformat(), 100 + i * 0.4, 5000])


def _setup(world, tmp_path, monkeypatch):
    s, a, _ = world
    d = tmp_path / a.isin
    d.mkdir()
    _prices(d / "prices__daily__2026-04-30.csv")
    (d / "news__announcements__2026-11-12.txt").write_text("Board approves buyback of shares today.")
    monkeypatch.setattr(feed_events, "complete", lambda *a_, **k: FeedItems(items=[
        FeedItem(headline="Buyback", quote="Board approves buyback of shares", event_type="deal", sentiment="positive", severity="medium")]))
    return s, a, d, {"local_dir": str(tmp_path)}


def test_feed_workers_are_created_and_report(world, stubs, tmp_path, monkeypatch):
    s, a, d, cfgd = _setup(world, tmp_path, monkeypatch)
    comp = _company(s, a)
    assert not s.scalars(select(Agent).where(Agent.parent_id == comp.id, Agent.role == "news")).all()
    res = cg.run_company_agent(s, comp.id, "r1", filings_config=cfgd)
    assert res.status == "completed"
    for role in ("technicals", "news", "social"):
        kinds = [e.kind for e in _events(s, _child(s, comp.id, role).id)]
        assert kinds[0] == "started" and kinds[-1] == "completed" and "output" in kinds
    out = _events(s, _child(s, comp.id, "news").id, ["output"])
    assert len(out) == 1 and out[0].payload["kind"] == "news_view" and out[0].payload["items"][0]["headline"] == "Buyback"
    assert s.scalars(select(Fact).where(Fact.instrument_id == a.id, Fact.metric == "momentum_3m")).first()
    soc = _events(s, _child(s, comp.id, "social").id, ["output"])[0].payload
    assert soc["kind"] == "social_view" and soc["items"] == []


def test_new_news_file_wakes_the_run_but_does_not_repeat_filing_extraction(world, stubs, tmp_path, monkeypatch):
    s, a, d, cfgd = _setup(world, tmp_path, monkeypatch)
    calls, _ = stubs
    comp = _company(s, a)
    assert cg.run_company_agent(s, comp.id, "r1", filings_config=cfgd).status == "completed"
    assert cg.run_company_agent(s, comp.id, "r2", filings_config=cfgd).status == "skipped"   # nothing changed
    extractions = [c for c in calls if c == ("fundamentals", a.id)]
    (d / "news__announcements__2026-11-13.txt").write_text("Company wins large deal from a European bank.")
    assert cg.run_company_agent(s, comp.id, "r3", filings_config=cfgd).status == "completed"  # woke up
    assert [c for c in calls if c == ("fundamentals", a.id)] == extractions                   # no re-extraction
    step = [e for e in _events(s, _child(s, comp.id, "fundamentals").id, ["step"])][-1]
    assert step.payload["skipped"] == "filings unchanged"
    assert cg.run_company_agent(s, comp.id, "r4", force=True, filings_config=cfgd).status == "completed"
    assert len([c for c in calls if c == ("fundamentals", a.id)]) == len(extractions) + 1     # force re-extracts


def test_company_without_a_feed_folder_still_runs(world, stubs):
    s, a, _ = world
    comp = _company(s, a)
    assert cg.run_company_agent(s, comp.id, "r1").status == "completed"
    out = _events(s, _child(s, comp.id, "technicals").id, ["output"])[0].payload
    assert out["files"] == 0 and out["saved"] == 0
