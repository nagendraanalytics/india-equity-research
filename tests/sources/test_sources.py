import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from ier.db.models import Agent, Instrument, Job, SeriesPoint, SourceState
from ier.runtime import events, queue, worker
from ier.sources import connectors, handlers
from ier.sources.registry import get_source, load_registry
from ier.sources.service import check_source, list_sources, schedule_due, seed_source_agents

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._b, self.text = status, body, text

    def json(self):
        return self._b


class FakeHttp:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def src(sid):
    return get_source(load_registry(), sid)


def test_registry_is_valid_and_every_segment_has_a_source():
    reg = load_registry()
    assert set(reg["segments"]) == {"company", "events", "market_flows", "sector_peers", "macro", "sentiment"}
    assert {s["segment"] for s in reg["sources"]} == set(reg["segments"])


def test_frankfurter_stores_points(session):
    body = {"rates": {"2026-11-12": {"INR": 88.1}, "2026-11-13": {"INR": 88.4}}}
    http = FakeHttp(Resp(200, body))
    st = check_source(session, src("fx_usdinr"), http=http, now=NOW, local_dir="")
    assert st.status == "connected" and st.last_count == 2
    assert http.calls[0][0].startswith("https://api.frankfurter.dev/v1/")
    pts = session.scalars(select(SeriesPoint).where(SeriesPoint.series_id == "usd_inr")).all()
    assert {p.day: p.value for p in pts}[date(2026, 11, 13)] == 88.4
    check_source(session, src("fx_usdinr"), http=http, now=NOW, local_dir="")   # idempotent upsert
    assert len(session.scalars(select(SeriesPoint)).all()) == 2


def test_network_errors_become_status_not_exceptions(session):
    st = check_source(session, src("fx_usdinr"), http=FakeHttp(ConnectionError("x")), now=NOW, local_dir="")
    assert st.status == "error" and "ConnectionError" in st.detail
    st = check_source(session, src("fx_usdinr"), http=FakeHttp(Resp(503)), now=NOW, local_dir="")
    assert st.status == "error" and "503" in st.detail


def test_fred_needs_key_and_never_leaks_it(session):
    st = check_source(session, src("fred_macro"), http=FakeHttp(Resp()), env={}, now=NOW, local_dir="")
    assert st.status == "needs_key" and "IER_FRED_API_KEY" in st.detail
    secret = "SECRETKEY123"
    st = check_source(session, src("fred_macro"), http=FakeHttp(RuntimeError(f"bad url key={secret}")),
                      env={"IER_FRED_API_KEY": secret}, now=NOW, local_dir="")
    assert st.status == "error" and secret not in (st.detail or "") and secret not in json.dumps(st.latest)
    st = check_source(session, src("fred_macro"), http=FakeHttp(Resp(403)),
                      env={"IER_FRED_API_KEY": secret}, now=NOW, local_dir="")
    assert st.status == "needs_key" and secret not in st.detail


def test_fred_points(session):
    body = {"observations": [{"date": "2026-11-12", "value": "4.1"}, {"date": "2026-11-11", "value": "."}]}
    st = check_source(session, src("fred_macro"), http=FakeHttp(Resp(200, body)),
                      env={"IER_FRED_API_KEY": "k"}, now=NOW, local_dir="")
    assert st.status == "connected"
    assert session.scalar(select(SeriesPoint.value).where(SeriesPoint.series_id == "us10y")) == 4.1


def test_only_allow_listed_hosts():
    ctx = connectors.Ctx(session=None, env={}, now=NOW, http=FakeHttp(Resp()))
    with pytest.raises(connectors.HostNotAllowed):
        connectors.http_get(ctx, "https://www.moneycontrol.com/x")
    with pytest.raises(connectors.HostNotAllowed):
        connectors.http_get(ctx, "http://api.frankfurter.dev/v1/latest")


def test_series_csv_waiting_then_loaded_then_stale(session, tmp_path):
    s = dict(src("nifty_it"), folder=str(tmp_path / "nifty"))
    st = check_source(session, s, now=NOW, local_dir="")
    assert st.status == "waiting_for_file"
    (tmp_path / "nifty").mkdir()
    (tmp_path / "nifty" / "a.csv").write_text('"Date ","Open ","Close "\n"13-Nov-2026","1","35,100.5"\n12-Nov-2026,1,35000\n')
    # header names carry trailing spaces in NSE exports
    st = check_source(session, s, now=NOW, local_dir="")
    assert st.status == "connected" and st.last_count == 2
    assert session.scalar(select(SeriesPoint.value).where(SeriesPoint.series_id == "nifty_it",
                                                          SeriesPoint.day == date(2026, 11, 13))) == 35100.5
    late = datetime(2026, 12, 20, tzinfo=timezone.utc)
    assert check_source(session, s, now=late, local_dir="").status == "stale"
    (tmp_path / "nifty" / "a.csv").write_text("Date,Close\nnot,a number\n")
    assert check_source(session, s, now=NOW, local_dir="").status == "error"


def test_local_files_statuses(session, tmp_path):
    s = src("announcements")
    assert check_source(session, s, now=NOW, local_dir="").status == "error"
    assert check_source(session, s, now=NOW, local_dir=str(tmp_path)).status == "waiting_for_file"
    d = tmp_path / "INE009A01021"
    d.mkdir()
    (d / "news__announcements__2026-11-12.txt").write_text("x")
    (d / "prices__daily__2026-11-12.csv").write_text("x")
    now = datetime.now(timezone.utc)
    st = check_source(session, s, now=now, local_dir=str(tmp_path))
    assert st.status == "connected" and st.last_count == 1
    f = check_source(session, src("filings_folder"), now=now, local_dir=str(tmp_path))
    assert f.status == "waiting_for_file"           # prices/news files are not filings
    (d / "results__Q2FY27__2026-10-16.pdf").write_text("x")
    assert check_source(session, src("filings_folder"), now=now, local_dir=str(tmp_path)).last_count == 1


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Infosys wins deal</title><pubDate>Thu, 12 Nov 2026 08:00:00 GMT</pubDate><source>Mint</source></item>
<item><title>Another headline</title><pubDate>Fri, 13 Nov 2026 08:00:00 GMT</pubDate></item></channel></rss>"""


def test_rss_writes_one_news_file_per_company(session, tmp_path):
    inst = Instrument(isin="INE009A01021", name="Infosys Ltd", nse_code="INFY")
    session.add(inst)
    session.flush()
    session.add(Agent(tier=5, role="company", scope_instrument_id=inst.id))
    session.flush()
    old = tmp_path / "INE009A01021"
    old.mkdir()
    (old / "news__rss-google__2026-11-01.txt").write_text("old")
    http = FakeHttp(Resp(200, text=RSS))
    st = check_source(session, src("news_rss"), http=http, now=NOW, local_dir=str(tmp_path))
    assert st.status == "connected" and st.last_count == 2
    files = sorted(p.name for p in old.iterdir())
    assert files == ["news__rss-google__2026-11-14.txt"]       # older rss file replaced
    body = (old / files[0]).read_text()
    assert "Infosys wins deal" in body and "headlines only" in body
    mtime = (old / files[0]).stat().st_mtime_ns
    check_source(session, src("news_rss"), http=http, now=NOW, local_dir=str(tmp_path))
    assert (old / files[0]).stat().st_mtime_ns == mtime         # unchanged content: no rewrite


def test_rss_rejects_entities(session, tmp_path):
    inst = Instrument(isin="INE009A01021", name="Infosys Ltd")
    session.add(inst)
    session.flush()
    session.add(Agent(tier=5, role="company", scope_instrument_id=inst.id))
    session.flush()
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel/></rss>'
    st = check_source(session, src("news_rss"), http=FakeHttp(Resp(200, text=bomb)), now=NOW,
                      local_dir=str(tmp_path))
    assert st.status in ("error", "waiting_for_file") and not list(tmp_path.iterdir())


def test_kite_status(session, tmp_path, monkeypatch):
    from ier.tools import zerodha
    monkeypatch.setattr(zerodha, "token_path", lambda cfg: tmp_path / "t.json")
    assert check_source(session, src("zerodha_prices"), now=NOW, local_dir="").status == "needs_key"
    zerodha.save_token(tmp_path / "t.json", "k", "tok", NOW)
    st = check_source(session, src("zerodha_prices"), now=NOW, local_dir="")
    assert st.status == "connected" and "tok" not in st.detail


def test_list_sources_groups_by_segment(session):
    check_source(session, src("fx_usdinr"), http=FakeHttp(Resp(200, {"rates": {"2026-11-13": {"INR": 88}}})),
                 now=NOW, local_dir="")
    out = list_sources(session)
    assert out["total"] == 14 and out["connected"] == 1
    macro = next(s for s in out["segments"] if s["id"] == "macro")
    assert macro["connected"] == 1 and macro["total"] == 2


def master(session):
    m = events.Agent(tier=1, role="master")
    session.add(m)
    session.flush()
    return m


def test_seed_source_agents_idempotent_and_shaped(factory):
    with factory() as s:
        with pytest.raises(ValueError):
            seed_source_agents(s)
        master(s)
        n = seed_source_agents(s)
        s.commit()
    with factory() as s:
        assert seed_source_agents(s) == 0
        segs = s.scalars(select(Agent).where(Agent.role == "segment")).all()
        srcs = s.scalars(select(Agent).where(Agent.role == "source")).all()
        assert len(segs) == 6 and len(srcs) == 14 and n == 20
        assert {a.tier for a in segs} == {queue.tier_number("segment")}
        assert {a.tier for a in srcs} == {queue.tier_number("source")}
        by_id = {a.id: a for a in segs}
        assert all(a.parent_id in by_id for a in srcs)
        node = events.tree(s)
        assert len(node.children) == 6          # controllers hang under the master


def test_schedule_due_queues_each_source_once_and_a_controller_job(factory):
    with factory() as s:
        master(s)
        seed_source_agents(s)
        s.commit()
        n = schedule_due(s, NOW)
        s.commit()
        assert n > 0
        assert schedule_due(s, NOW) == 0              # already queued: nothing stacked
        jobs = s.scalars(select(Job)).all()
        assert all(j.trigger == "schedule" for j in jobs)
        manual_only = {a.id for a in s.scalars(select(Agent).where(Agent.role == "source"))
                       if get_source(load_registry(), a.scope_name).get("refresh_minutes", 0) == 0}
        assert not {j.agent_id for j in jobs} & manual_only   # refresh 0 = Test button only
        ctrl_jobs = [j for j in jobs if j.depends_on is not None]
        assert ctrl_jobs and all(s.get(Agent, j.agent_id).role == "segment" for j in ctrl_jobs)


def test_handlers_run_through_the_worker(factory, tmp_path, monkeypatch):
    from ier.sources import service
    monkeypatch.setattr(service, "_local_dir", lambda: str(tmp_path))
    with factory() as s:
        master(s)
        seed_source_agents(s)
        agent = s.scalar(select(Agent).where(Agent.scope_name == "filings_folder"))
        job = queue.enqueue(s, agent.id, "manual")
        s.commit()
        aid = agent.id
    reg = {"source": handlers.run_source_agent, "segment": handlers.run_segment_agent}
    with factory() as s:
        j = queue.claim_next(s, "source")
        s.commit()
        worker._run_job(s, j, reg, __import__("threading").Event(), None)
        s.commit()
    with factory() as s:
        st = s.get(SourceState, "filings_folder")
        assert st.status == "waiting_for_file"
        assert s.get(Job, job.id).status == "done"          # waiting is not a failure
        ev = events.AgentEvent
        out = s.scalar(select(ev).where(ev.agent_id == aid, ev.kind == "output"))
        assert out.payload["status"] == "waiting_for_file"


def test_a_source_error_fails_the_agent_job(factory, monkeypatch):
    from ier.sources import service
    monkeypatch.setattr(service, "_local_dir", lambda: "")
    with factory() as s:
        master(s)
        seed_source_agents(s)
        agent = s.scalar(select(Agent).where(Agent.scope_name == "filings_folder"))   # no local_dir -> error
        job = queue.enqueue(s, agent.id, "manual", )
        s.commit()
    with factory() as s:
        j = queue.claim_next(s, "source")
        s.commit()
        worker._run_job(s, j, {"source": handlers.run_source_agent}, __import__("threading").Event(), None)
        s.commit()
    with factory() as s:
        assert s.get(Job, job.id).status in ("queued", "failed")   # retried with backoff, not done
        assert s.get(Job, job.id).last_error


def test_segment_controller_summarises_its_sources(factory):
    with factory() as s:
        master(s)
        seed_source_agents(s)
        s.add(SourceState(source_id="fx_usdinr", status="connected"))
        ctrl = s.scalar(select(Agent).where(Agent.role == "segment", Agent.scope_name == "macro"))
        queue.enqueue(s, ctrl.id, "manual")
        s.commit()
        cid = ctrl.id
    with factory() as s:
        j = queue.claim_next(s, "segment")
        s.commit()
        worker._run_job(s, j, {"segment": handlers.run_segment_agent}, __import__("threading").Event(), None)
        s.commit()
        out = s.scalar(select(events.AgentEvent).where(events.AgentEvent.agent_id == cid,
                                                       events.AgentEvent.kind == "output"))
        assert out.payload["connected"] == 1 and out.payload["of"] == 2 and out.payload["affects_score"] is False
