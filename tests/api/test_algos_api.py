import csv
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from ier.algos.core import AlgoResult
from ier.algos.store import save_results
from ier.db.models import Instrument, Scorecard
from tests.api.conftest import TODAY
from tests.api.test_api import FORBIDDEN, commit

ISIN = "INE009A01021"
BAD_WORDS = ("buy", "sell")


def _words(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _words(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _words(v)
    elif isinstance(obj, str):
        yield obj


def _seed(factory, isin=ISIN, macro="Information Technology", scores=None):
    def go(s):
        i = Instrument(isin=isin, name=f"Co {isin[-4:]}", nse_code=isin[-4:], macro_sector=macro, sector="IT Services")
        s.add(i)
        s.flush()
        res = scores or {"mom_6m": AlgoResult(40, 5.0, "6-month return: 5.00%"),
                         "mom_12m_skip_1m": AlgoResult(80, 20.0, "x"),
                         "mom_3m": AlgoResult(70, 9.0, "y"),
                         "pe_ratio": AlgoResult(None, None, "needs trailing four-quarter EPS")}
        save_results(s, i.id, TODAY - timedelta(days=1), res, None, "r1")
        s.add(Scorecard(instrument_id=i.id, run_date=TODAY, long_term_score=61.0, short_term_score=55.0,
                        rules_long_score=61.0, rules_short_score=55.0, algo_long_score=60.0, algo_short_score=70.0,
                        algo_weight=0.0, confidence="low"))
        return i.id
    return commit(factory, go)


def test_catalogue_lists_macro_sectors(client, factory):
    _seed(factory)
    _seed(factory, "INE003A01015", macro="Healthcare")
    assert client.get("/algos").json()["macro_sectors"] == ["Healthcare", "Information Technology"]


def test_catalogue(client):
    b = client.get("/algos").json()
    ids = {i["id"] for i in b["items"]}
    assert len(ids) == 33 and "rsi14" in ids and "pe_ratio" in ids
    assert b["blend_weight"] == 0.0 and b["max_blend_weight"] == 0.3
    rsi = next(i for i in b["items"] if i["id"] == "rsi14")
    assert rsi["has_chart"] and rsi["family"] == "momentum" and rsi["band"] == [70.0, 30.0]
    assert not next(i for i in b["items"] if i["id"] == "pe_ratio")["has_chart"]


def test_stock_algos(client, factory):
    _seed(factory)
    b = client.get(f"/stocks/{ISIN}/algos").json()
    assert b["isin"] == ISIN and b["as_of"] == (TODAY - timedelta(days=1)).isoformat()
    assert b["composite"]["long"] == 60.0 and b["composite"]["short"] == 70.0
    assert b["rules"] == {"long": 61.0, "short": 55.0}
    assert b["with_algorithms"] == {"long": 61.0, "short": 55.0, "weight": 0.0}
    by = {i["algo_id"]: i for i in b["items"]}
    assert by["mom_3m"]["family"] == "momentum" and by["pe_ratio"]["score"] is None
    assert by["pe_ratio"]["reason"].startswith("needs")


def test_stock_algos_unknown_isin_404(client):
    assert client.get("/stocks/INE000000000/algos").status_code == 404


def test_stock_without_scores_is_empty_not_error(client, factory):
    commit(factory, lambda s: s.add(Instrument(isin=ISIN, name="X", nse_code="XX")))
    b = client.get(f"/stocks/{ISIN}/algos").json()
    assert b["items"] == [] and b["composite"] is None


def _prices(root, n=300):
    d = root / ISIN
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "prices__daily__2026-11-01.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Close", "Volume"])
        for i in range(n):
            w.writerow([(TODAY - timedelta(days=n - 1 - i)).isoformat(), 100 + i * 0.2 + (i % 9), 5000])


def test_series_lengths_match(make_app, factory, tmp_path):
    _seed(factory)
    _prices(tmp_path)
    from starlette.testclient import TestClient
    with TestClient(make_app(config={"local_dir": str(tmp_path)})) as c:
        r = c.get(f"/stocks/{ISIN}/algos/macd_histogram/series?points=100")
        assert r.status_code == 200
        b = r.json()
        assert len(b["dates"]) == 100 and b["caption"] and len(b["panels"]) == 2
        for p in b["panels"]:
            for s in p["series"]:
                assert len(s["values"]) == 100
        assert c.get(f"/stocks/{ISIN}/algos/nope/series").status_code == 404
        assert c.get(f"/stocks/{ISIN}/algos/macd_histogram/series?points=5").status_code == 422
        stub = c.get(f"/stocks/{ISIN}/algos/pe_ratio/series").json()
        assert stub["panels"] == [] and stub["caption"].startswith("needs")


def test_series_short_history_explains(make_app, factory, tmp_path):
    _seed(factory)
    _prices(tmp_path, n=20)
    from starlette.testclient import TestClient
    with TestClient(make_app(config={"local_dir": str(tmp_path)})) as c:
        b = c.get(f"/stocks/{ISIN}/algos/price_vs_sma200/series").json()
        assert b["panels"] == [] and "needs 200" in b["caption"]


def test_series_without_price_files(client, factory):
    _seed(factory)
    b = client.get(f"/stocks/{ISIN}/algos/mom_3m/series").json()
    assert b["panels"] == [] and b["caption"].startswith("needs")


def test_sector_algos(client, factory):
    _seed(factory)
    _seed(factory, "INE002A01018")
    _seed(factory, "INE003A01015", macro="Healthcare")
    b = client.get("/sectors/Information Technology/algos").json()
    assert {i["isin"] for i in b["items"]} == {ISIN, "INE002A01018"}
    it = b["items"][0]
    assert it["composite"]["long"] == 60.0 and "momentum" in it["family_scores"]
    assert client.get("/sectors/Nonexistent/algos").json()["items"] == []


def test_universe_for_one_algorithm(client, factory):
    _seed(factory)
    _seed(factory, "INE002A01018")
    b = client.get("/algos/mom_3m/universe").json()
    assert len(b["items"]) == 2 and b["items"][0]["score"] == 70.0 and b["algo"]["id"] == "mom_3m"
    assert client.get("/algos/nope/universe").status_code == 404


def test_wording_guard(client, factory):
    _seed(factory)
    for url in ("/algos", f"/stocks/{ISIN}/algos", "/sectors/Information Technology/algos", "/algos/rsi14/universe"):
        body = client.get(url).json()
        text = list(_words(body))
        keys = {t for t in text}
        assert keys.isdisjoint(FORBIDDEN)
        assert not any(w in t.lower().split() for t in text for w in BAD_WORDS), url
