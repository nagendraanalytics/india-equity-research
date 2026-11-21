from ier.sources import connectors


def test_sources_lists_all_segments_not_checked(client):
    r = client.get("/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 14 and body["connected"] == 0 and body["affects_score"] is False
    assert {s["id"] for s in body["segments"]} == {"company", "events", "market_flows", "sector_peers",
                                                    "macro", "sentiment"}
    assert all(o["status"] == "not_checked" for s in body["segments"] for o in s["sources"])


def test_check_unknown_source_is_404(client):
    assert client.post("/sources/nope/check").status_code == 404


def test_check_runs_the_connector_and_stores_state(client, monkeypatch):
    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"rates": {"2026-11-13": {"INR": 88.4}}}

    class H:
        def get(self, url, params=None):
            return R()

    monkeypatch.setattr(connectors, "default_http", lambda: H())
    r = client.post("/sources/fx_usdinr/check")
    assert r.status_code == 200 and r.json()["status"] == "connected"
    assert client.get("/sources").json()["connected"] == 1


def test_check_needs_key_without_leaking(client, monkeypatch):
    monkeypatch.delenv("IER_FRED_API_KEY", raising=False)
    r = client.post("/sources/fred_macro/check")
    assert r.json()["status"] == "needs_key"


def test_segment_and_source_agents_can_be_rerun(client, factory):
    from sqlalchemy import select
    from ier.db.models import Agent
    from ier.sources.service import seed_source_agents
    with factory() as s:
        s.add(Agent(tier=1, role="master"))
        s.flush()
        seed_source_agents(s)
        s.commit()
        ids = [s.scalar(select(Agent.id).where(Agent.role == r)) for r in ("segment", "source")]
    for i in ids:
        assert client.get(f"/agents/{i}").json()["rerunnable"] is True
        r = client.post(f"/agents/{i}/rerun")
        assert r.status_code == 200 and r.json()["job_id"]
