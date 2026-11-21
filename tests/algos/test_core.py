import pytest

from ier.algos.config import load_algo_config
from ier.algos.core import AlgoResult, linear_score
from ier.algos.registry import Algorithm, all_algorithms, register


def test_linear_score():
    assert linear_score(5, 0, 10) == 50
    assert linear_score(15, 0, 10) == 100
    assert linear_score(-1, 0, 10) == 0
    assert linear_score(2, 10, 0) == 80


def test_registry_rejects_duplicate_id(monkeypatch):
    from ier.algos import registry
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    a = Algorithm("dup_test_algo", "Dup", "trend", "long", 1, "d", lambda ctx: AlgoResult(None, None, "x"), None)
    register(a)
    with pytest.raises(ValueError):
        register(a)
    assert any(x.id == "dup_test_algo" for x in all_algorithms())


def test_default_config_has_zero_blend():
    cfg = load_algo_config()
    assert cfg["blend_weight"] == 0.0


def test_blend_weight_validation(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text("blend_weight: 0.5\n")
    cfg = load_algo_config(p)
    assert cfg["blend_weight"] == 0.30
    assert cfg["notes"]
    p.write_text("blend_weight: -0.1\n")
    with pytest.raises(ValueError):
        load_algo_config(p)
    p.write_text("blend_weight: x\n")
    with pytest.raises(ValueError):
        load_algo_config(p)
