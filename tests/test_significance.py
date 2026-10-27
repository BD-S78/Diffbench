import math
import random

from diffbench.significance import (
    MIN_MEANINGFUL_CHANGE_PCT,
    Outcome,
    compare_percentiles,
    effective_iterations,
)


def _latencies(seed: int, n: int, median_ms: float, spread: float = 0.35) -> list[float]:
    """Log-normal samples: long right tail, like real latency data."""
    rng = random.Random(seed)
    return [median_ms * math.exp(rng.gauss(0, spread)) for _ in range(n)]


def test_identical_distributions_give_no_clear_difference():
    # The key guarantee: same server vs itself must not declare a winner.
    a = _latencies(1, 200, 20.0)
    b = _latencies(2, 200, 20.0)
    result = compare_percentiles(a, b)
    assert result[50].outcome is Outcome.NO_CLEAR_DIFFERENCE
    assert result[50].ci_low_pct < 0 < result[50].ci_high_pct


def test_false_positive_rate_is_near_five_percent():
    # Across many independent identical-server runs, a 95% interval should
    # wrongly claim a p50 winner only rarely (expected ~5%).
    claims = sum(
        compare_percentiles(
            _latencies(100 + i, 100, 20.0),
            _latencies(500 + i, 100, 20.0),
            percentiles=(50,),
            iterations=400,
        )[50].outcome
        in (Outcome.FASTER, Outcome.SLOWER)
        for i in range(60)
    )
    assert claims <= 8  # generous bound (~13%) to keep the test stable


def test_clearly_faster_candidate_is_detected():
    result = compare_percentiles(_latencies(3, 100, 20.0), _latencies(4, 100, 10.0))
    assert result[50].outcome is Outcome.FASTER
    assert result[50].pct_change < 0
    assert result[50].ci_high_pct < 0


def test_clearly_slower_candidate_is_detected():
    result = compare_percentiles(_latencies(5, 100, 10.0), _latencies(6, 100, 20.0))
    assert result[50].outcome is Outcome.SLOWER
    assert result[50].ci_low_pct > 0


def test_tiny_but_consistent_change_is_negligible():
    # Very stable endpoints (~0.1% spread) where B is 1% slower: the noise
    # test can detect it, but 1% is below the "worth reporting" threshold.
    a = _latencies(7, 300, 20.0, spread=0.001)
    b = _latencies(8, 300, 20.2, spread=0.001)
    result = compare_percentiles(a, b, percentiles=(50,))
    assert result[50].ci_low_pct > 0  # beyond noise...
    assert abs(result[50].pct_change) < MIN_MEANINGFUL_CHANGE_PCT  # ...but tiny
    assert result[50].outcome is Outcome.NEGLIGIBLE


def test_small_shift_in_noisy_data_is_within_noise():
    # The same 1% shift buried in normal latency spread is indistinguishable
    # from chance at n=300, so no claim is made at all.
    a = _latencies(9, 300, 20.0)
    b = _latencies(10, 300, 20.2)
    assert compare_percentiles(a, b, percentiles=(50,))[50].outcome is (
        Outcome.NO_CLEAR_DIFFERENCE
    )


def test_percentiles_need_enough_samples():
    # n=100 is enough for p50/p90 but not p95 (200) or p99 (1000).
    result = compare_percentiles(_latencies(8, 100, 20.0), _latencies(9, 100, 10.0))
    assert result[90].outcome is Outcome.FASTER
    assert result[95].outcome is Outcome.TOO_FEW_SAMPLES
    assert result[99].outcome is Outcome.TOO_FEW_SAMPLES
    # The observed values are still reported, just not judged.
    assert result[99].pct_change is not None
    assert result[99].ci_low_pct is None


def test_tiny_samples_never_declare_a_winner():
    result = compare_percentiles(_latencies(10, 10, 20.0), _latencies(11, 10, 5.0))
    assert all(c.outcome is Outcome.TOO_FEW_SAMPLES for c in result.values())


def test_missing_data_on_either_side():
    result = compare_percentiles(_latencies(12, 50, 20.0), [])
    assert all(c.outcome is Outcome.NO_DATA for c in result.values())
    assert result[50].b_ms is None
    assert result[50].pct_change is None


def test_results_are_deterministic():
    a, b = _latencies(13, 150, 20.0), _latencies(14, 150, 19.0)
    assert compare_percentiles(a, b) == compare_percentiles(a, b)


def test_zero_latency_baseline_never_claims_a_winner():
    result = compare_percentiles([0.0] * 50, [5.0] * 50, percentiles=(50,))
    assert result[50].outcome is Outcome.NO_CLEAR_DIFFERENCE
    assert result[50].pct_change is None


def test_iterations_scale_down_for_large_runs():
    assert effective_iterations(2000, 100) == 2000
    assert effective_iterations(2000, 1000) == 1000
    assert effective_iterations(2000, 1_000_000) == 200  # floor
