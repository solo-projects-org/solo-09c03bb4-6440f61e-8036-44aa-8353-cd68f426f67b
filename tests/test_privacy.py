"""Property-oriented tests for the Laplace mechanism and budget arithmetic."""

from __future__ import annotations

import math
import random
import statistics
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.privacy import (  # noqa: E402
    MAX_RELEASED_COUNT,
    canonical_decimal,
    canonical_epsilon,
    draw_laplace,
    epsilon_scale,
    make_noisy_count,
    request_fingerprint,
)


class SeededRng:
    """``random.Random`` with the two methods the mechanism uses."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def random(self) -> float:
        return self._rng.random()


def test_scale_is_inverse_epsilon():
    assert epsilon_scale(Decimal("0.1")) == pytest.approx(10.0)
    assert epsilon_scale(Decimal("0.5")) == pytest.approx(2.0)
    assert epsilon_scale(Decimal("1")) == pytest.approx(1.0)


def test_laplace_is_symmetric_and_scaled():
    rng = SeededRng(7)
    scale = 10.0
    samples = [draw_laplace(scale, rng) for _ in range(40_000)]
    assert abs(statistics.mean(samples)) < 0.35
    assert statistics.median(samples) == pytest.approx(0.0, abs=0.15)
    # Variance of Laplace(0, b) is 2 b^2.
    assert statistics.variance(samples) == pytest.approx(
        2 * scale * scale, rel=0.05
    )


def test_laplace_rejects_invalid_scales():
    for scale in (0.0, -1.0, math.inf, math.nan):
        with pytest.raises(ValueError):
            draw_laplace(scale)


def test_noisy_count_is_within_reasonable_range_and_clamped():
    rng = SeededRng(3)
    values = [make_noisy_count(0, Decimal("0.01")) for _ in range(200)]
    assert all(isinstance(value, int) for value in values)
    assert min(values) >= 0
    assert max(values) <= MAX_RELEASED_COUNT


def test_canonical_epsilon_equates_equivalent_decimals():
    assert canonical_epsilon(Decimal("0.1")) == canonical_epsilon(Decimal("0.10"))
    assert canonical_epsilon(Decimal("1.0")) == "1"
    assert canonical_epsilon(Decimal("0.000001")) == "0.000001"
    assert canonical_epsilon(Decimal("1E-9")) == canonical_epsilon(
        Decimal("0.000000001")
    )


def test_canonical_epsilon_rejects_zero_and_negatives():
    for value in (Decimal("0"), Decimal("-0.1"), Decimal("NaN"), Decimal("Inf")):
        with pytest.raises(ValueError):
            canonical_epsilon(value)


def test_fingerprint_depends_on_every_parameter():
    epsilon = Decimal("0.1")
    base = request_fingerprint("north", 1, epsilon)
    assert base == request_fingerprint("north", 1, Decimal("0.10"))
    assert base != request_fingerprint("south", 1, epsilon)
    assert base != request_fingerprint("north", 0, epsilon)
    assert base != request_fingerprint("north", 1, Decimal("0.2"))
    # Field separator prevents "ab"+"c" colliding with "a"+"bc".
    assert request_fingerprint("a", 1, epsilon) != request_fingerprint(
        "a\x1f1", 1, epsilon
    )


def test_canonical_decimal_never_returns_negative_zero():
    assert canonical_decimal(Decimal("0")) == "0"
    assert canonical_decimal(Decimal("0.0")) == "0"
    assert canonical_decimal(Decimal("-0.0")) == "0"
    assert canonical_decimal(Decimal("0.9")) == "0.9"
