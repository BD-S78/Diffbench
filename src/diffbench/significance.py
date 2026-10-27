"""Decides whether a latency difference between two targets is real or noise.

Method: percentile bootstrap.

To ask "could a difference this big appear by chance if A and B were really
the same?", we simulate many plausible re-runs of the benchmark using only
the data already collected: draw a same-sized random sample *with
replacement* from A's latencies and from B's, compute the percentile change
from A to B, and repeat a few thousand times. The middle 95% of those
simulated changes is the 95% confidence interval (CI).

* CI entirely below 0  -> B is faster (beyond run-to-run noise).
* CI entirely above 0  -> B is slower.
* CI includes 0        -> no clear difference at this sample size.
* Beyond noise but under MIN_MEANINGFUL_CHANGE_PCT -> negligible.

Calibration (identical simulated servers, n=100, 200 trials): a winner was
falsely claimed at p50 in 4.5% of trials, in line with a 95% interval.

This adapts to how noisy each endpoint is and to sample size: few or noisy
samples give a wide interval and an honest "can't tell".

Limits: it only accounts for *random* noise. Systematic biases (different
environments, caching differences, cold starts, request ordering) are
handled elsewhere (interleaving, ABBA ordering, warm-up) or documented.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from diffbench.stats import REPORTED_PERCENTILES, min_samples_for_percentile, percentile

CONFIDENCE = 0.95
DEFAULT_ITERATIONS = 2000
# Lower bound when the iteration count is scaled down for large runs.
MIN_ITERATIONS = 200
# Cap on resampled values per target (iterations x samples), so the analysis
# stays quick even for very large runs (n=1000 -> 1000 iterations).
MAX_RESAMPLED_VALUES = 1_000_000
# Fixed seed: the same measurements always produce the same verdict.
DEFAULT_SEED = 0
# Smallest change worth calling "faster"/"slower". With very large samples a
# 1% wobble can pass the noise test, but nobody acts on it (and CI gating
# shouldn't fail on it). Below this, a real-but-tiny change is NEGLIGIBLE.
MIN_MEANINGFUL_CHANGE_PCT = 2.0


class Outcome(str, Enum):
    FASTER = "faster"
    SLOWER = "slower"
    # Beyond noise, but smaller than MIN_MEANINGFUL_CHANGE_PCT.
    NEGLIGIBLE = "negligible"
    NO_CLEAR_DIFFERENCE = "no_clear_difference"
    TOO_FEW_SAMPLES = "too_few_samples"
    NO_DATA = "no_data"


@dataclass(frozen=True)
class PercentileComparison:
    """Result of comparing one percentile between Target A and Target B."""

    q: int
    a_ms: Optional[float]
    b_ms: Optional[float]
    # Percent change from A to B (negative = B is faster).
    pct_change: Optional[float]
    ci_low_pct: Optional[float]
    ci_high_pct: Optional[float]
    outcome: Outcome
    # Successful samples per target needed before this percentile is tested.
    min_samples: int


def _pct_change(a: float, b: float) -> float:
    return (b - a) / a * 100


def effective_iterations(requested: int, n_samples: int) -> int:
    """Scale the bootstrap down for large samples to bound runtime."""
    budget = MAX_RESAMPLED_VALUES // max(n_samples, 1)
    return max(min(requested, budget), min(requested, MIN_ITERATIONS))


def _bootstrap_cis(
    a: list[float],
    b: list[float],
    qs: Sequence[int],
    *,
    iterations: int,
    seed: int,
) -> dict[int, tuple[float, float]]:
    """Bootstrap CIs (in percent change) for several percentiles at once.

    One set of resamples is shared by all percentiles: faster, and keeps the
    percentiles' intervals consistent with each other.
    """
    rng = random.Random(seed)
    n_a, n_b = len(a), len(b)
    iterations = effective_iterations(iterations, max(n_a, n_b))
    changes: dict[int, list[float]] = {q: [] for q in qs}

    for _ in range(iterations):
        resample_a = sorted(rng.choices(a, k=n_a))
        resample_b = sorted(rng.choices(b, k=n_b))
        for q in qs:
            pa = percentile(resample_a, q)
            if pa > 0:
                changes[q].append(_pct_change(pa, percentile(resample_b, q)))

    tail = (1 - CONFIDENCE) / 2 * 100
    cis: dict[int, tuple[float, float]] = {}
    for q, values in changes.items():
        if values:
            values.sort()
            cis[q] = (percentile(values, tail), percentile(values, 100 - tail))
    return cis


def _no_data(q: int, a: list[float], b: list[float]) -> PercentileComparison:
    return PercentileComparison(
        q=q,
        a_ms=percentile(a, q) if a else None,
        b_ms=percentile(b, q) if b else None,
        pct_change=None,
        ci_low_pct=None,
        ci_high_pct=None,
        outcome=Outcome.NO_DATA,
        min_samples=min_samples_for_percentile(q),
    )


def _classify(low: float, high: float, observed_change: float) -> Outcome:
    """Map a CI (and the observed change) to an outcome.

    Requires BOTH a CI that excludes 0 (beyond random noise) AND an observed
    change of at least MIN_MEANINGFUL_CHANGE_PCT (big enough to matter).
    """
    if low <= 0 <= high:
        return Outcome.NO_CLEAR_DIFFERENCE
    if abs(observed_change) < MIN_MEANINGFUL_CHANGE_PCT:
        return Outcome.NEGLIGIBLE
    return Outcome.FASTER if high < 0 else Outcome.SLOWER


def compare_percentiles(
    latencies_a: Sequence[float],
    latencies_b: Sequence[float],
    *,
    percentiles: Sequence[int] = REPORTED_PERCENTILES,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> dict[int, PercentileComparison]:
    """Compare A vs B at each percentile, deciding real difference vs noise.

    A percentile is only tested once each target has enough successful
    samples to estimate it (see ``min_samples_for_percentile``); below that
    it is reported as ``TOO_FEW_SAMPLES`` instead of a misleading verdict.
    """
    a = sorted(latencies_a)
    b = sorted(latencies_b)
    if not a or not b:
        return {q: _no_data(q, a, b) for q in percentiles}

    n_min = min(len(a), len(b))
    observed = {q: (percentile(a, q), percentile(b, q)) for q in percentiles}
    testable = [
        q
        for q in percentiles
        if n_min >= min_samples_for_percentile(q) and observed[q][0] > 0
    ]
    cis = (
        _bootstrap_cis(a, b, testable, iterations=iterations, seed=seed)
        if testable
        else {}
    )

    results: dict[int, PercentileComparison] = {}
    for q in percentiles:
        pa, pb = observed[q]
        low = high = None
        if n_min < min_samples_for_percentile(q):
            outcome = Outcome.TOO_FEW_SAMPLES
        elif q in cis:
            low, high = cis[q]
            outcome = _classify(low, high, _pct_change(pa, pb))
        else:
            # Degenerate data (e.g. a 0 ms baseline): never claim a winner.
            outcome = Outcome.NO_CLEAR_DIFFERENCE
        results[q] = PercentileComparison(
            q=q,
            a_ms=pa,
            b_ms=pb,
            pct_change=_pct_change(pa, pb) if pa > 0 else None,
            ci_low_pct=low,
            ci_high_pct=high,
            outcome=outcome,
            min_samples=min_samples_for_percentile(q),
        )
    return results
