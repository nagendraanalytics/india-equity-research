from datetime import date

import pytest

from ier.db.models import Source
from ier.tools.base import FetchRefused
from ier.tools.exchange_filings import list_filings
from ier.tools.local_filings import ingest_local, list_local

ISIN = "INE009A01021"


def _pdf(text: str) -> bytes:
    """A minimal one-page PDF whose text layer is `text`."""
    stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return out


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "filings" / ISIN
    d.mkdir(parents=True)
    return tmp_path / "filings", d


def test_lists_files_newest_first_and_ignores_bad_names(folder):
    root, d = folder
    (d / "results__Q1FY27__2026-07-15.txt").write_text("Revenue 100")
    (d / "results__Q2FY27__2026-10-16.txt").write_text("Revenue 110")
    (d / "annual_report__FY2025.txt").write_text("x")
    (d / "notes.txt").write_text("bad name")
    (d / "results__Q3FY27__2027-01-15.exe").write_text("bad type")
    refs = list_local(ISIN, str(root))
    assert [(r.filing_type, r.period, r.published_date) for r in refs] == [
        ("results", "Q2FY27", date(2026, 10, 16)),
        ("results", "Q1FY27", date(2026, 7, 15)),
        ("annual_report", "FY2025", None),
    ]
    assert all(r.url.startswith("file:") for r in refs)


def test_bad_isin_or_missing_folder_gives_empty(folder):
    root, _ = folder
    assert list_local("../etc", str(root)) == []
    assert list_local("INE000000000", str(root)) == []


def test_list_filings_uses_local_dir_over_web_index(folder, session):
    root, d = folder
    (d / "results__Q2FY27__2026-10-16.txt").write_text("Revenue 110")
    refs = list_filings(ISIN, session=session,
                        config={"local_dir": str(root), "index_url": "https://x.invalid/{isin}",
                                "allowed_hosts": {}})
    assert [r.period for r in refs] == ["Q2FY27"]


def test_ingest_txt_html_and_pdf_set_tier_and_detect_change(folder, session):
    root, d = folder
    (d / "results__Q1FY27.txt").write_text("Revenue 4,17,640 crore")
    (d / "results__Q2FY27.html").write_text(
        "<html><script>var x=1</script><p>Net profit &amp; margin 25</p></html>")
    (d / "annual_report__FY2025.pdf").write_bytes(_pdf("EBITDA 9876 crore"))
    by_period = {r.period: r for r in list_local(ISIN, str(root))}

    txt = ingest_local(by_period["Q1FY27"].url, session, local_dir=str(root), tier=1)
    assert b"4,17,640" in txt.content and txt.changed
    src = session.get(Source, txt.source_id)
    assert (src.tier, src.type, src.status) == (1, "local", "ok")

    html = ingest_local(by_period["Q2FY27"].url, session, local_dir=str(root))
    assert b"Net profit & margin 25" in html.content and b"var x" not in html.content
    assert session.get(Source, html.source_id).tier == 2  # conservative default

    pdf = ingest_local(by_period["FY2025"].url, session, local_dir=str(root))
    assert b"EBITDA 9876" in pdf.content

    again = ingest_local(by_period["Q1FY27"].url, session, local_dir=str(root), tier=1)
    assert again.source_id == txt.source_id and not again.changed
    (d / "results__Q1FY27.txt").write_text("Revenue 5,00,000 crore")
    assert ingest_local(by_period["Q1FY27"].url, session, local_dir=str(root), tier=1).changed


def test_corrupt_pdf_and_empty_file_are_stale_not_fatal(folder, session):
    root, d = folder
    (d / "results__Q1FY27.pdf").write_bytes(b"not a pdf")
    (d / "results__Q2FY27.txt").write_text("   ")
    for ref in list_local(ISIN, str(root)):
        res = ingest_local(ref.url, session, local_dir=str(root))
        assert res.content == b"" and not res.changed
        assert session.get(Source, res.source_id).status == "stale"


def test_refuses_paths_outside_root_symlinks_and_bad_inputs(folder, session, tmp_path):
    root, d = folder
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    with pytest.raises(FetchRefused):
        ingest_local(outside.as_uri(), session, local_dir=str(root))
    link = d / "results__Q1FY27.txt"
    link.symlink_to(outside)
    assert list_local(ISIN, str(root)) == []            # symlinks are never listed
    with pytest.raises(FetchRefused):
        ingest_local(link.as_uri(), session, local_dir=str(root))
    with pytest.raises(FetchRefused):
        ingest_local("https://example.com/a.txt", session, local_dir=str(root))
    with pytest.raises(FetchRefused):
        ingest_local(outside.as_uri(), session, local_dir=str(tmp_path / "nope"))
    big = d / "results__Q2FY27.txt"
    big.write_text("x" * 50)
    with pytest.raises(FetchRefused):
        ingest_local(big.as_uri(), session, local_dir=str(root), max_bytes=10)
    with pytest.raises(ValueError):
        ingest_local(big.as_uri(), session, local_dir=str(root), tier=4)
