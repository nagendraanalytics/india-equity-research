from pathlib import Path

import pytest
from sqlalchemy import func, select

from ier.db.models import Instrument
from ier.taxonomy.loader import load_classification

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "classification_sample.csv"


def _get(session, isin):
    return session.scalars(select(Instrument).where(Instrument.isin == isin)).one()


def test_loads_four_levels(session):
    load_classification(SAMPLE, session)
    tcs = _get(session, "INE467B01029")
    assert tcs.macro_sector == "Information Technology"
    assert tcs.sector == "Information Technology"
    assert tcs.industry == "IT - Software"
    assert tcs.basic_industry == "Computers - Software & Consulting"
    assert tcs.classification_status == "classified"


def test_dual_listed_merged_by_isin(session):
    n = load_classification(SAMPLE, session)
    assert n == 5
    assert session.scalar(select(func.count()).select_from(Instrument)) == 5
    infy = _get(session, "INE009A01021")
    assert infy.nse_code == "INFY"
    assert infy.bse_code == "500209"


def test_unclassified_company_flagged(session):
    load_classification(SAMPLE, session)
    inst = _get(session, "INE000X01011")
    assert inst.classification_status == "missing"
    assert inst.basic_industry is None
    assert inst.bse_code == "539999"
    assert inst.nse_code is None


def test_reload_is_idempotent(session):
    load_classification(SAMPLE, session)
    load_classification(SAMPLE, session)
    assert session.scalar(select(func.count()).select_from(Instrument)) == 5
    assert _get(session, "INE009A01021").nse_code == "INFY"


HEADER = "isin,nse_code,bse_code,name,exchange,macro_sector,sector,industry,basic_industry\n"
IT = "Information Technology,Information Technology,IT - Software,"


def _write(tmp_path, *lines):
    p = tmp_path / "c.csv"
    p.write_text(HEADER + "\n".join(lines) + "\n")
    return p


def test_conflicting_basic_industry_for_same_isin_raises(session, tmp_path):
    p = _write(
        tmp_path,
        f"INE009A01021,INFY,,Infosys Limited,NSE,{IT}Computers - Software & Consulting",
        f"INE009A01021,,500209,Infosys Limited,BSE,{IT}Other Software",
    )
    with pytest.raises(ValueError) as exc:
        load_classification(p, session)
    msg = str(exc.value)
    assert "INE009A01021" in msg and "basic_industry" in msg and "line 3" in msg


def test_complementary_nse_bse_rows_still_merge(session, tmp_path):
    p = _write(
        tmp_path,
        f"INE009A01021,INFY,,Infosys Limited,NSE,{IT}Computers - Software & Consulting",
        "INE009A01021,,500209,Infosys Limited,BSE,,,,",
    )
    assert load_classification(p, session) == 1
    infy = _get(session, "INE009A01021")
    assert (infy.nse_code, infy.bse_code) == ("INFY", "500209")
    assert infy.classification_status == "classified"
