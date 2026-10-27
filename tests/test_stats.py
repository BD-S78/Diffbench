import statistics

import pytest

from diffbench.stats import Stats, min_samples_for_percentile, percentile


def test_from_latencies_basic():
    stats = Stats.from_latencies([10.0, 20.0, 30.0], success_count=3, error_count=0)
    assert stats.count == 3
    assert stats.success_count == 3
    assert stats.error_count == 0
    assert stats.min_ms == 10.0
    assert stats.max_ms == 30.0
    assert stats.mean_ms == 20.0
    assert stats.p50_ms == 20.0


def test_percentiles_on_known_values():
    # 1..100: linear interpolation puts p50 at 50.5 and p99 at 99.01.
    stats = Stats.from_latencies(
        [float(v) for v in range(100, 0, -1)], success_count=100, error_count=0
    )
    assert stats.p50_ms == pytest.approx(50.5)
    assert stats.p90_ms == pytest.approx(90.1)
    assert stats.p95_ms == pytest.approx(95.05)
    assert stats.p99_ms == pytest.approx(99.01)


def test_percentile_matches_stdlib_inclusive_quantiles():
    values = sorted([3.0, 7.5, 1.2, 9.9, 4.4, 6.1, 2.8, 8.3])
    expected = statistics.quantiles(values, n=100, method="inclusive")
    for q in (10, 25, 50, 75, 90, 99):
        assert percentile(values, q) == pytest.approx(expected[q - 1])


def test_percentile_single_sample():
    assert percentile([42.0], 99) == 42.0
    stats = Stats.from_latencies([42.0], success_count=1, error_count=0)
    assert stats.p50_ms == stats.p99_ms == 42.0


def test_percentile_rejects_empty_and_out_of_range():
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1.0], 101)


def test_min_samples_for_percentile():
    assert min_samples_for_percentile(50) == 20
    assert min_samples_for_percentile(90) == 100
    assert min_samples_for_percentile(95) == 200
    assert min_samples_for_percentile(99) == 1000


def test_from_latencies_with_errors():
    stats = Stats.from_latencies([10.0, 10.0], success_count=2, error_count=3)
    assert stats.count == 5
    assert stats.success_count == 2
    assert stats.error_count == 3
    assert stats.mean_ms == 10.0


def test_from_latencies_empty_has_no_latency_values():
    # A fully failing target must not look like a 0 ms ("fast") target.
    stats = Stats.from_latencies([], success_count=0, error_count=4)
    assert stats.count == 4
    assert stats.success_count == 0
    assert stats.error_count == 4
    assert stats.error_rate == 1.0
    assert not stats.has_latency_data
    for value in (stats.min_ms, stats.mean_ms, stats.p50_ms, stats.p99_ms, stats.max_ms):
        assert value is None


def test_throughput_counts_successful_requests_per_second():
    stats = Stats.from_latencies(
        [10.0] * 8, success_count=8, error_count=2, duration_s=2.0
    )
    assert stats.duration_s == 2.0
    # Only successful requests count toward throughput: 8 / 2s.
    assert stats.throughput_rps == 4.0


def test_throughput_is_zero_without_duration():
    stats = Stats.from_latencies([10.0], success_count=1, error_count=0)
    assert stats.duration_s == 0.0
    assert stats.throughput_rps == 0.0
