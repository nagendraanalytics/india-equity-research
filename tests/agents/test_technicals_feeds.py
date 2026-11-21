import csv
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from ier.agents import feed_events
from ier.agents.feed_events import FeedItem, FeedItems, analyse_feed
from ier.agents.technicals import compute_metrics, extract_technicals, parse_prices
from ier.db.models import Fact, Instrument, Source
from ier.tools.feeds import feeds_signature, list_feed_files, read_feed_text
from ier.tools.local_filings import list_local

ISIN = "INE009A01021"


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / ISIN
    d.mkdir()
    return tmp_path, d


def write_prices(path, n=260, start=100.0, step=0.5, header=("Date", "Close", "Volume")):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        d0 = date(2026, 1, 1)
        for i in range(n):
            w.writerow([(d0 + timedelta(days=i)).isoformat(), f"{start + step * i:.2f}", 1000 + i])


def test_parse_prices_accepts_nse_style_headers_dates_and_commas(tmp_path):
    text = "DATE1, CLOSE_PRICE ,TTL_TRD_QNTY\n13-Nov-2026,\"1,520.50\",\"2,000\"\n12-Nov-2026,1500,1000\nbad,row,x\n"
    rows = parse_prices(text)
    assert [(d.isoformat(), c) for d, c, _ in rows] == [("2026-11-12", 1500.0), ("2026-11-13", 1520.5)]
    assert rows[1][2] == 2000.0
    assert parse_prices("foo,bar\n1,2\n") == []


def test_compute_metrics_known_values_and_missing_history():
    rows = [(date(2026, 1, 1) + timedelta(days=i), 100.0 + i, 1000.0) for i in range(260)]
    m = compute_metrics(rows)
    last = 359.0
    assert m["price_vs_sma200"] == pytest.approx((last / (sum(range(100 + 60, 100 + 260)) / 200) - 1) * 100, abs=1e-3)
    assert m["momentum_3m"] == pytest.approx((359.0 / (100 + 196) - 1) * 100, abs=1e-3)
    assert m["rsi14"] == 100.0           # only rises: no losses
    assert m["volume_ratio_20d"] == pytest.approx(1.0)
    short = compute_metrics(rows[:30])
    assert set(short) == {"rsi14", "volume_ratio_20d"} or set(short) == {"rsi14"}
    assert compute_metrics([]) == {}


def test_extract_technicals_saves_tier2_facts_once(session, folder):
    root, d = folder
    write_prices(d / "prices__daily__2026-09-17.csv")
    inst = Instrument(isin=ISIN, name="Infosys")
    session.add(inst); session.flush()
    r = extract_technicals(session, inst.id, ISIN, str(root))
    assert r["files"] == 1 and r["saved"] >= 4 and r["as_of"] == "2026-09-17"
    facts = {f.metric: f for f in session.scalars(select(Fact).where(Fact.instrument_id == inst.id))}
    assert {"price_vs_sma200", "momentum_3m", "rsi14"} <= set(facts)
    src = session.get(Source, facts["rsi14"].source_id)
    assert src.type == "price" and src.tier == 2
    assert extract_technicals(session, inst.id, ISIN, str(root))["saved"] == 0     # idempotent
    assert extract_technicals(session, inst.id, ISIN, None)["files"] == 0          # no folder: nothing


def test_feed_files_are_not_filings_and_change_the_signature(session, folder):
    root, d = folder
    (d / "results__Q2FY27__2026-10-16.txt").write_text("Revenue was 100.")
    (d / "news__announcements__2026-11-12.txt").write_text("Auditor resigned today.")
    write_prices(d / "prices__daily__2026-09-17.csv", n=5)
    (d / "social__x__2026-11-12.json").write_text("{}")
    assert [r.filing_type for r in list_local(ISIN, str(root))] == ["results"]   # feeds never ingested
    assert [p.name for p in list_feed_files(ISIN, str(root), "news")] == ["news__announcements__2026-11-12.txt"]
    sig = feeds_signature(ISIN, str(root))
    assert len(sig) == 3
    (d / "news__announcements__2026-11-12.txt").write_text("Auditor resigned today. Another item.")
    assert feeds_signature(ISIN, str(root)) != sig
    assert feeds_signature(ISIN, None) == []
    assert "Auditor" in read_feed_text(d / "news__announcements__2026-11-12.txt")


def test_news_items_need_a_quote_that_is_really_in_the_file(session, folder, monkeypatch):
    root, d = folder
    (d / "news__announcements__2026-11-12.txt").write_text(
        "Board approves buyback of shares. Company announces new CFO appointment effective next month.")
    items = FeedItems(items=[
        FeedItem(headline="Buyback approved", quote="Board approves buyback of shares", event_type="deal", sentiment="positive", severity="medium"),
        FeedItem(headline="Invented fraud", quote="SEBI orders forensic audit of accounts", event_type="governance", sentiment="negative", severity="high"),
        FeedItem(headline="Odd values", quote="new CFO appointment effective", event_type="weird", sentiment="great", severity="huge")])
    monkeypatch.setattr(feed_events, "complete", lambda *a, **k: items)
    v = analyse_feed(session, ISIN, "news", "r1", str(root))
    assert [i["headline"] for i in v.items] == ["Buyback approved", "Odd values"] and v.dropped == 1
    odd = v.items[1]
    assert (odd["event_type"], odd["sentiment"], odd["severity"]) == ("other", "neutral", "low")
    assert all(i["verified"] for i in v.items)
    p = feed_events.view_payload(v, "r1")
    assert p["kind"] == "news_view" and p["counts"]["positive"] == 1


def test_social_items_are_marked_unverified_and_no_folder_is_empty(session, folder, monkeypatch):
    root, d = folder
    (d / "social__x__2026-11-12.txt").write_text("Rumour: management exit soon says anonymous user.")
    monkeypatch.setattr(feed_events, "complete", lambda *a, **k: FeedItems(items=[
        FeedItem(headline="Exit rumour", quote="management exit soon", event_type="management", sentiment="negative", severity="medium")]))
    v = analyse_feed(session, ISIN, "social", "r1", str(root))
    assert v.items and v.items[0]["verified"] is False
    assert analyse_feed(session, ISIN, "social", "r1", None).items == []
