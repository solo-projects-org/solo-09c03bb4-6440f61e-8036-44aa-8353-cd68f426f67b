"""Laplace mechanism for a single histogram cell.

The mechanism is a textbook Laplace mechanism for a histogram query: the
count for (region, outcome) has global sensitivity 1, so adding
``Laplace(scale = 1 / epsilon)`` yields epsilon-differential privacy for that
answer.  Epsilon is charged from a single global budget (1.0 by default);
each *successful, newly answered* query spends its epsilon, and retries of
the same request id never spend budget again.
"""

from __future__ import annotations

import hashlib
import math
import secrets
from decimal import Decimal

#: Bound on the decimal exponent we accept.  Rejecting absurd exponents keeps
#: ``1 / epsilon`` finite as a float and avoids precision-based denial of
#: service.
MIN_EPSILON = Decimal("1E-300")

#: Largest value a released count may take.  With a tiny epsilon the Laplace
#: sample can be astronomically large; clamping the *output* is pure
#: post-processing and therefore preserves differential privacy, while keeping
#: the value inside SQLite's 64-bit integer range.
MAX_RELEASED_COUNT = (1 << 63) - 1


def canonical_decimal(value: Decimal) -> str:
    """Render a decimal in plain notation with no negative zero."""
    if value == 0:
        return "0"
    normalised = value.normalize()
    text = format(normalised, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def canonical_epsilon(value: Decimal) -> str:
    """Return a stable textual form of an epsilon.

    ``Decimal`` treats ``0.10`` and ``0.1`` as equal; rendering through the
    canonical form means the same logical parameter always hashes the same and
    is stored identically, so a retry is recognised as a retry.
    """
    if not value.is_finite() or value <= 0:
        raise ValueError("epsilon must be a positive finite number")
    if value < MIN_EPSILON:
        raise ValueError("epsilon is too small to be represented")
    return canonical_decimal(value)


def epsilon_scale(epsilon: Decimal) -> float:
    """Laplace scale ``1 / epsilon`` as a float (sensitivity is 1)."""
    canonical_epsilon(epsilon)  # validation only
    scale = 1.0 / float(epsilon)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("epsilon produces a non-finite Laplace scale")
    return scale


def request_fingerprint(region: str, outcome: int, epsilon: Decimal) -> str:
    """Hash the full parameter set a request id is bound to.

    Used to detect a client reusing one request id with different parameters,
    which must be rejected rather than answered from the old result.
    """
    payload = "\x1f".join((region, str(outcome), canonical_epsilon(epsilon)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def draw_laplace(scale: float, rng: secrets.SystemRandom | None = None) -> float:
    """Draw a Laplace(0, scale) sample.

    Inverse-CDF transform on a uniform in the *open* interval (0, 1): ``u``
    must never be exactly 0 or 1, otherwise the logarithm diverges.
    ``SystemRandom`` draws from the OS CSPRNG, the right source of randomness
    for a privacy mechanism.
    """
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Laplace scale must be a positive finite number")
    rng = rng or secrets.SystemRandom()
    while True:
        u = rng.random()
        if 0.0 < u < 1.0:
            break
    if u < 0.5:
        return scale * math.log(2.0 * u)
    return -scale * math.log(2.0 * (1.0 - u))


def make_noisy_count(true_count: int, epsilon: Decimal) -> int:
    """Release an integer noisy count for ``true_count`` at ``epsilon``.

    The Laplace scale is ``1 / epsilon`` (sensitivity 1).  The sample is
    rounded to the nearest integer so the public answer is a plain count;
    post-processing a differentially private value preserves the guarantee.
    Counts are clamped to ``[0, MAX_RELEASED_COUNT]``: a negative number of
    people is not a meaningful release, and the upper clamp keeps the stored
    value inside SQLite's signed 64-bit integer range.
    """
    if true_count < 0:
        raise ValueError("true_count must be non-negative")
    value = true_count + draw_laplace(epsilon_scale(epsilon))
    return min(MAX_RELEASED_COUNT, max(0, int(round(value))))
