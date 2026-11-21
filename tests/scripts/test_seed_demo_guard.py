import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "seed_demo", Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py")
seed_demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed_demo)

H = "postgresql+psycopg://postgres@localhost:5433/"


@pytest.mark.parametrize("url", [H + "ier_demo", "postgresql+psycopg://u@127.0.0.1/ier_demo",
                                 "postgresql+psycopg://u@localhost/x_demo"])
def test_accepts_local_demo_databases(url):
    seed_demo.assert_demo_db(url)


@pytest.mark.parametrize("url", [
    H + "ier_latest", H + "ier_test", H + "ier", H + "ier_demo_backup", H + "demo", H + "ier_demo_test",
    "postgresql+psycopg://u@db.example.com/ier_demo", "postgresql+psycopg:///ier_demo",
    "postgresql+psycopg://u@10.0.0.5/ier_demo",
    "postgresql+psycopg://u@localhost/ier_demo?host=evil.com",
    "postgresql+psycopg://u@localhost/ier_demo?host=/var/run/postgresql",
    "postgresql+psycopg://u@localhost/ier_demo?hostaddr=10.0.0.1",
    "postgresql+psycopg://u@localhost/ier_demo?dbname=prod",
    "postgresql+psycopg://u@localhost/ier_demo?service=prod",
    "postgresql+psycopg://u@LOCALHOST/IER_DEMO", "postgresql+psycopg://u@127.0.0.1.evil.com/ier_demo",
    "postgresql+psycopg://localhost@evil.com/ier_demo"])
def test_refuses_everything_else(url):
    with pytest.raises(SystemExit):
        seed_demo.assert_demo_db(url)
