import pytest

from ier.algos.blend import blend_scores


def test_blend_zero_weight_identity():
    assert blend_scores(63.3, 90.0, 0.0) == 63.3


def test_blend_math():
    assert blend_scores(60, 80, 0.1) == pytest.approx(62.0)


def test_weight_clamped_to_cap():
    assert blend_scores(60, 100, 0.9) == blend_scores(60, 100, 0.30) == pytest.approx(72.0)


def test_negative_weight_clamped_to_zero():
    assert blend_scores(60, 100, -1) == 60


def test_missing_algo_uses_rules_only():
    assert blend_scores(60, None, 0.3) == 60


def test_rules_none_stays_none():
    assert blend_scores(None, 80, 0.3) is None
