"""Tests for the Unicode sparkline helper."""
from __future__ import annotations
import pytest
from armature.report.sparkline import sparkline


def test_empty_returns_empty_string():
    assert sparkline([]) == ""


def test_single_value_returns_single_block():
    result = sparkline([0.5])
    assert len(result) == 1


def test_all_equal_values_returns_same_block():
    result = sparkline([0.7, 0.7, 0.7])
    assert len(set(result)) == 1  # all same character


def test_ascending_series_increases():
    result = sparkline([0.1, 0.5, 0.9])
    assert result[0] <= result[1] <= result[2]


def test_descending_series_decreases():
    result = sparkline([0.9, 0.5, 0.1])
    assert result[0] >= result[1] >= result[2]


def test_output_length_matches_input():
    values = [0.1, 0.4, 0.6, 0.8, 0.3]
    assert len(sparkline(values)) == len(values)


def test_values_clamped_to_zero_one():
    # Should not raise even with out-of-range input
    result = sparkline([-0.5, 1.5, 0.5])
    assert len(result) == 3


def test_returns_unicode_block_characters():
    blocks = "▁▂▃▄▅▆▇█"
    result = sparkline([0.2, 0.8])
    for ch in result:
        assert ch in blocks
