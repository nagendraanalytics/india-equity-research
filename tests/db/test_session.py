import threading
import time

from ier.db import session as db_session


def test_get_engine_created_once_under_concurrency(monkeypatch):
    created = []

    def slow_create_engine(url, **kw):
        time.sleep(0.05)  # widen the race window
        created.append(url)
        return object()

    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_factory", None)
    monkeypatch.setattr(db_session, "create_engine", slow_create_engine)
    monkeypatch.setattr(db_session, "sessionmaker", lambda eng, **kw: ("factory", eng))
    barrier = threading.Barrier(8)
    got = []

    def call():
        barrier.wait()
        got.append(db_session.get_engine())

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(created) == 1 and len({id(e) for e in got}) == 1
    assert db_session._factory == ("factory", got[0])
