from armature.state.leverage import _pearson_r


def test_pearson_perfect_positive():
    assert abs(_pearson_r([1, 2, 3, 4], [2, 4, 6, 8]) - 1.0) < 1e-9


def test_pearson_perfect_negative():
    assert abs(_pearson_r([1, 2, 3, 4], [8, 6, 4, 2]) - (-1.0)) < 1e-9


def test_pearson_zero_variance_returns_none():
    assert _pearson_r([1, 1, 1], [1, 2, 3]) is None
    assert _pearson_r([1, 2, 3], [5, 5, 5]) is None


def test_pearson_too_short_returns_none():
    assert _pearson_r([1], [2]) is None
    assert _pearson_r([], []) is None


def test_pearson_uncorrelated_near_zero():
    r = _pearson_r([1, 2, 3, 4, 5, 6, 7, 8], [3, 8, 2, 7, 4, 1, 6, 5])
    assert r is not None and abs(r) < 0.5  # weak/no linear relation