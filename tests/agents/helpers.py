from ier.db.models import Chunk, Document, Instrument, Source


def make_instrument(session, isin="INE009A01021", name="Infosys Ltd"):
    i = Instrument(isin=isin, name=name)
    session.add(i)
    session.flush()
    return i


def make_chunk(session, instrument, text, tier=1, url=None):
    src = Source(url=url or f"https://x.invalid/{tier}/{abs(hash(text))}", type="filing", tier=tier)
    session.add(src)
    session.flush()
    doc = Document(source_id=src.id, instrument_id=instrument.id, title="t")
    session.add(doc)
    session.flush()
    ch = Chunk(document_id=doc.id, source_id=src.id, ordinal=0, text=text)
    session.add(ch)
    session.flush()
    return ch
