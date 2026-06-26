"""Tiny dependency-free statistics: Spearman rho, permutation p, bootstrap CI.

Uses a seeded `random.Random` so results are reproducible (no global RNG state).
"""
from __future__ import annotations

import random
from statistics import mean, stdev


def _rank(values: list[float]) -> list[float]:
    """Average ranks (1-indexed), with ties sharing the mean rank."""
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0          # average of 1-indexed positions i+1..j+1
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    if sx == 0 or sy == 0:
        return 0.0
    return num / (sx * sy)


def spearman_rho(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    return _pearson(_rank(xs), _rank(ys))


def permutation_p(xs: list[float], ys: list[float], *, seed: int = 12345, n: int = 5000) -> float:
    """Two-sided permutation p-value for |Spearman rho|."""
    observed = abs(spearman_rho(xs, ys))
    rng = random.Random(seed)
    count = 0
    ys_copy = list(ys)
    for _ in range(n):
        rng.shuffle(ys_copy)
        if abs(spearman_rho(xs, ys_copy)) >= observed:
            count += 1
    return (count + 1) / (n + 1)            # +1 smoothing


def bootstrap_ci(diffs: list[float], *, seed: int = 12345, n: int = 5000,
                 alpha: float = 0.05) -> tuple[float, float, float]:
    """Return (mean, lower, upper) of the bootstrap distribution of the mean."""
    if not diffs:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    m = len(diffs)
    means: list[float] = []
    for _ in range(n):
        sample = [diffs[rng.randrange(m)] for _ in range(m)]
        means.append(mean(sample))
    means.sort()
    lo_idx = int((alpha / 2) * n)
    hi_idx = int((1 - alpha / 2) * n) - 1
    return (mean(diffs), means[lo_idx], means[hi_idx])