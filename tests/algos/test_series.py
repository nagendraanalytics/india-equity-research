from datetime import date

from ier.algos.series import clean_bars, load_price_bars

ISIN = "INE009A01021"


def test_clean_bars_drops_bad_and_dedups():
    rows = [
        (date(2026, 1, 2), 10.0, 5.0),
        (date(2026, 1, 1), 0.0, 5.0),
        (date(2026, 1, 3), float("nan"), 5.0),
        (date(2026, 1, 2), 11.0, 6.0),
        (date(2026, 1, 4), 12.0, -3.0),
    ]
    bars = clean_bars(rows)
    assert [(b.day.day, b.close) for b in bars] == [(2, 11.0), (4, 12.0)]
    assert bars[1].volume is None


def test_load_price_bars_reads_files(tmp_path):
    d = tmp_path / ISIN
    d.mkdir()
    (d / "prices__daily__2026-11-13.csv").write_text("Date,Close,Volume\n2026-11-12,100,10\n2026-11-13,101,12\n")
    bars, n = load_price_bars(ISIN, str(tmp_path))
    assert len(bars) == 2 and n == 1 and bars[-1].close == 101


def test_load_price_bars_no_dir_or_files():
    assert load_price_bars(ISIN, None) == ([], None)
    assert load_price_bars(ISIN, "/nonexistent") == ([], None)
