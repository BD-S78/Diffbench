import math
import random

from diffbench.report import (
    has_verdict_data,
    render_header,
    render_table,
    render_verdict,
    render_warnings,
)
from diffbench.significance import compare_percentiles
from diffbench.stats import Stats


def _latencies(seed: int, n: int, median_ms: float) -> list[float]:
    rng = random.Random(seed)
    return [median_ms * math.exp(rng.gauss(0, 0.3)) for _ in range(n)]


def _stats(latencies, errors=0, duration_s=0.0):
    return Stats.from_latencies(
        latencies, success_count=len(latencies), error_count=errors, duration_s=duration_s
    )


def _report(lat_a, lat_b, errors_a=0, errors_b=0):
    """Build (stats_a, stats_b, comparisons) the same way the CLI does."""
    return (
        _stats(lat_a, errors_a),
        _stats(lat_b, errors_b),
        compare_percentiles(lat_a, lat_b),
    )


def _row(table: str, metric: str) -> str:
    return next(line for line in table.splitlines() if line.startswith(f"| {metric}"))


# --- table ---------------------------------------------------------------


def test_table_has_percentile_rows_and_no_median():
    a, b, cmp = _report(_latencies(1, 100, 20), _latencies(2, 100, 10))
    table = render_table(a, b, cmp)
    for metric in ("p50 Latency", "p90 Latency", "p95 Latency", "p99 Latency (Tail)"):
        assert metric in table
    assert "Median" not in table


def test_percentile_rows_are_labelled_by_noise_test():
    a, b, cmp = _report(_latencies(3, 100, 20), _latencies(4, 100, 10))
    table = render_table(a, b, cmp)
    assert "(Faster)" in _row(table, "p50 Latency")
    assert "(too few samples)" in _row(table, "p99 Latency")


def test_noise_level_difference_is_not_labelled_faster_or_slower():
    a, b, cmp = _report(_latencies(5, 200, 20), _latencies(6, 200, 20))
    row = _row(render_table(a, b, cmp), "p50 Latency")
    assert "(within noise)" in row
    assert "Faster" not in row and "Slower" not in row


def test_untested_rows_carry_no_direction_claim():
    a, b, cmp = _report(_latencies(7, 100, 20), _latencies(8, 100, 10))
    table = render_table(a, b, cmp)
    for metric in ("Min Latency", "Mean Latency", "Max Latency"):
        row = _row(table, metric)
        assert "Faster" not in row and "Slower" not in row


def test_fully_failing_target_shows_na_not_minus_100_percent():
    # Regression: B with zero successes used to show 0 ms and "-100% (Faster)".
    a, b, cmp = _report(_latencies(9, 50, 20), [], errors_b=50)
    table = render_table(a, b, cmp)
    assert "-100" not in table
    assert "Faster" not in table
    assert "n/a" in _row(table, "p50 Latency")


def test_throughput_rows_only_when_requested():
    lat = _latencies(10, 50, 20)
    table = render_table(_stats(lat), _stats(lat))
    assert "Throughput" not in table and "Duration" not in table


def test_throughput_increase_is_labelled_faster():
    lat = _latencies(11, 10, 20)
    table = render_table(
        _stats(lat, duration_s=2.0), _stats(lat, duration_s=1.0), include_throughput=True
    )
    assert "+100.0% (Faster)" in _row(table, "Throughput")
    assert "-50.0% (Faster)" in _row(table, "Duration")


# --- header & warnings ---------------------------------------------------


def test_header_shows_mode_counts_and_warmup():
    header = render_header("simultaneous", 50, 10, warmup=10)
    assert "simultaneous" in header
    assert "50 request(s) per target" in header
    assert "concurrency 10 per target" in header
    assert "warmup 10 untimed per target" in header
    assert "warmup" not in render_header("interleaved", 50, 1, warmup=0)


def test_partial_failures_produce_a_warning():
    a, b, _ = _report(_latencies(12, 100, 20), _latencies(13, 90, 20), errors_b=10)
    warnings = render_warnings(a, b, requests=100)
    assert any("Target B: 10/100 requests failed (10%)" in w for w in warnings)
    assert not any("Target A" in w for w in warnings)


def test_small_sample_note():
    a, b, _ = _report(_latencies(14, 20, 20), _latencies(15, 20, 20))
    assert any("small sample" in w for w in render_warnings(a, b, requests=20))
    assert not any("small sample" in w for w in render_warnings(a, b, requests=100))


# --- verdict -------------------------------------------------------------


def test_verdict_faster_includes_confidence_interval():
    verdict = render_verdict(*_report(_latencies(16, 100, 20), _latencies(17, 100, 10)))
    assert "Target B is faster than Target A" in verdict
    assert "95% CI" in verdict


def test_verdict_slower():
    verdict = render_verdict(*_report(_latencies(18, 100, 10), _latencies(19, 100, 20)))
    assert "Target B is slower than Target A" in verdict


def test_verdict_identical_servers_is_no_clear_difference():
    verdict = render_verdict(*_report(_latencies(20, 200, 20), _latencies(21, 200, 20)))
    assert "No clear difference" in verdict
    assert "faster than" not in verdict and "slower than" not in verdict


def test_verdict_mentions_p99_needs_more_samples():
    verdict = render_verdict(*_report(_latencies(22, 100, 20), _latencies(23, 100, 10)))
    assert "p99 not compared (needs 1000+ samples per target)" in verdict


def test_verdict_small_sample_refuses_to_pick_a_winner():
    verdict = render_verdict(*_report(_latencies(24, 10, 20), _latencies(25, 10, 5)))
    assert "Not enough data for a verdict" in verdict


def test_verdict_when_a_target_has_no_successes():
    stats_a, stats_b, cmp = _report(_latencies(26, 30, 20), [], errors_b=30)
    assert not has_verdict_data(stats_a, stats_b)
    verdict = render_verdict(stats_a, stats_b, cmp)
    assert "No speed comparison possible" in verdict
    assert "Target B" in verdict and "Target A had" not in verdict
    assert "faster" not in verdict.lower()
