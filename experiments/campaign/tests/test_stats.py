import math
from campaign_runner import stats


def test_rank_handles_ties_with_average_ranks():
    assert stats._rank([3.0, 1.0, 2.0, 2.0]) == [4.0, 1.0, 2.5, 2.5]


def test_spearman_perfect_monotone_is_neg1():
    xs = [1, 2, 3, 4, 5]
    ys = [5, 4, 3, 2, 1]
    assert abs(stats.spearman_rho(xs, ys) - (-1.0)) < 1e-9


def test_spearman_perfect_positive_is_plus1():
    xs = [1, 2, 3, 4, 5]
    ys = [10, 20, 30, 40, 50]
    assert abs(stats.spearman_rho(xs, ys) - 1.0) < 1e-9


def test_permutation_p_strong_signal_is_small():
    xs = list(range(20))
    ys = list(range(20))[::-1]            # perfect negative
    p = stats.permutation_p(xs, ys, seed=1, n=2000)
    assert p <= 0.05                       # strong effect => significant


def test_bootstrap_ci_covers_mean():
    diffs = [0.1] * 100
    mean, lo, hi = stats.bootstrap_ci(diffs, seed=1, n=1000)
    assert abs(mean - 0.1) < 1e-9
    assert lo <= 0.1 <= hi


def test_bootstrap_ci_negative_for_negative_diffs():
    mean, lo, hi = stats.bootstrap_ci([-0.2] * 100, seed=1, n=1000)
    assert mean < 0 and hi < 0