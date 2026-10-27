import asyncio

import pytest

from diffbench.runner import run_interleaved as _run_interleaved_async
from diffbench.runner import run_simultaneous as _run_simultaneous_async


# The runners are async; these thin wrappers let the tests stay plain
# synchronous functions without adding a pytest-asyncio dependency.
def run_interleaved(*args, **kwargs):
    return asyncio.run(_run_interleaved_async(*args, **kwargs))


def run_simultaneous(*args, **kwargs):
    return asyncio.run(_run_simultaneous_async(*args, **kwargs))


# --------------------------------------------------------------------------
# Interleaved mode
# --------------------------------------------------------------------------


def test_run_interleaved_success(fast_server, slow_server):
    result_a, result_b = run_interleaved(fast_server, slow_server, requests=5)
    assert result_a.url == fast_server
    assert result_b.url == slow_server
    assert len(result_a.results) == 5
    assert len(result_b.results) == 5
    assert result_a.success_count == 5
    assert result_b.success_count == 5
    assert result_a.error_count == 0
    assert result_b.error_count == 0
    assert all(latency >= 0 for latency in result_a.latencies_ms)
    assert all(latency >= 0 for latency in result_b.latencies_ms)


def test_run_interleaved_records_failures(fast_server, failing_server):
    result_a, result_b = run_interleaved(fast_server, failing_server, requests=3)
    assert result_a.success_count == 3
    assert result_b.success_count == 0
    assert result_b.error_count == 3
    # Failed requests are excluded from latency stats.
    assert result_b.latencies_ms == []


def test_run_interleaved_slower_server_has_higher_latency(fast_server, slow_server):
    result_fast, result_slow = run_interleaved(fast_server, slow_server, requests=5)

    fast_mean = sum(result_fast.latencies_ms) / len(result_fast.latencies_ms)
    slow_mean = sum(result_slow.latencies_ms) / len(result_slow.latencies_ms)

    assert slow_mean > fast_mean


def test_run_interleaved_uses_abba_order(labeled_server_pair):
    # The server must see ABBA order (A B, B A, A B, B A): interleaved so
    # both targets share one time window, and alternating who goes first so
    # neither always benefits from the other warming a shared cache.
    url_a, url_b, hit_log = labeled_server_pair
    run_interleaved(url_a, url_b, requests=4)
    assert hit_log == ["A", "B", "B", "A", "A", "B", "B", "A"]


def test_abba_order_alternates_who_goes_first():
    from diffbench.runner import abba_order

    assert [abba_order(i) for i in range(4)] == [(0, 1), (1, 0), (0, 1), (1, 0)]


def test_run_interleaved_warmup_is_untimed_and_not_counted(labeled_server_pair):
    url_a, url_b, hit_log = labeled_server_pair
    result_a, result_b = run_interleaved(url_a, url_b, requests=3, warmup=2)
    # Server saw warm-up + measured requests...
    assert hit_log.count("A") == 5
    assert hit_log.count("B") == 5
    # ...but only the measured ones are recorded ("-n 3 means 3").
    assert len(result_a.results) == 3
    assert len(result_b.results) == 3


def test_run_interleaved_handles_connection_error():
    # Nothing listens on this port, so httpx should raise and we should
    # record it as a failed request rather than crashing.
    result_a, result_b = run_interleaved(
        "http://127.0.0.1:1/", "http://127.0.0.1:2/", requests=2, timeout=1.0
    )
    assert result_a.success_count == 0
    assert result_a.error_count == 2
    assert all(r.error is not None for r in result_a.results)
    assert result_b.success_count == 0
    assert result_b.error_count == 2
    assert all(r.error is not None for r in result_b.results)


def test_run_interleaved_never_overlaps_targets(tracked_server_pair):
    # The whole point of interleaved mode: A and B are never in flight at
    # the same moment (no artificial shared-DB contention).
    url_a, url_b, tracker = tracked_server_pair(delay_seconds=0.02)
    run_interleaved(url_a, url_b, requests=5)
    assert tracker.counts["A"] == 5
    assert tracker.counts["B"] == 5
    assert tracker.peak_total == 1


def test_run_interleaved_records_shared_duration(fast_server, slow_server):
    result_a, result_b = run_interleaved(fast_server, slow_server, requests=3)
    assert result_a.duration_s > 0
    # Both targets ran inside one alternating window, so they share it.
    assert result_a.duration_s == result_b.duration_s


# --------------------------------------------------------------------------
# Simultaneous mode
# --------------------------------------------------------------------------


def test_run_simultaneous_sends_exactly_n_per_target(tracked_server_pair):
    url_a, url_b, tracker = tracked_server_pair(delay_seconds=0.01)
    result_a, result_b = run_simultaneous(url_a, url_b, requests=20, concurrency=4)
    # The server saw exactly N per target -- no more, no less.
    assert tracker.counts["A"] == 20
    assert tracker.counts["B"] == 20
    assert len(result_a.results) == 20
    assert len(result_b.results) == 20
    assert result_a.success_count == 20
    assert result_b.success_count == 20


def test_run_simultaneous_respects_concurrency_per_target(tracked_server_pair):
    url_a, url_b, tracker = tracked_server_pair(delay_seconds=0.1)
    run_simultaneous(url_a, url_b, requests=12, concurrency=4)
    # Each target sees exactly `concurrency` in flight at peak: the cap is
    # honoured (never more) and actually reached (real concurrency).
    assert tracker.peak["A"] == 4
    assert tracker.peak["B"] == 4


def test_run_simultaneous_overlaps_targets(tracked_server_pair):
    # Both targets are loaded at the same time, so the combined in-flight
    # count exceeds what either target has alone.
    url_a, url_b, tracker = tracked_server_pair(delay_seconds=0.1)
    run_simultaneous(url_a, url_b, requests=12, concurrency=4)
    assert tracker.peak_total > 4


def test_run_simultaneous_is_actually_parallel(tracked_server_pair):
    # 10 requests x 100ms at concurrency 5 is ~2 waves (~0.2-0.35s). Run
    # serially it would take >= 1s. We check the *measured* window
    # (duration_s), not total wall-clock, because one-off client setup
    # (SSL context loading, ~0.5s+ per client on some machines) happens
    # before measurement starts and is deliberately excluded.
    url_a, url_b, _ = tracked_server_pair(delay_seconds=0.1)
    result_a, result_b = run_simultaneous(url_a, url_b, requests=10, concurrency=5)
    assert result_a.duration_s < 0.7
    assert result_b.duration_s < 0.7


def test_run_simultaneous_targets_get_equal_measurement_windows(tracked_server_pair):
    # Regression test: client setup for one target must not run while the
    # other target's requests are in flight. That used to block the event
    # loop and inflate Target A's duration/latency (~0.9s vs ~0.3s for B
    # against identical servers).
    url_a, url_b, _ = tracked_server_pair(delay_seconds=0.1)
    result_a, result_b = run_simultaneous(url_a, url_b, requests=10, concurrency=5)
    longer = max(result_a.duration_s, result_b.duration_s)
    shorter = min(result_a.duration_s, result_b.duration_s)
    assert longer < shorter * 1.5


def test_run_simultaneous_caps_concurrency_at_requests(tracked_server_pair):
    url_a, url_b, tracker = tracked_server_pair(delay_seconds=0.05)
    run_simultaneous(url_a, url_b, requests=3, concurrency=50)
    assert tracker.counts["A"] == 3
    assert tracker.counts["B"] == 3
    assert tracker.peak["A"] <= 3
    assert tracker.peak["B"] <= 3


def test_run_simultaneous_records_failures(fast_server, failing_server):
    result_a, result_b = run_simultaneous(
        fast_server, failing_server, requests=6, concurrency=3
    )
    assert result_a.success_count == 6
    assert result_b.success_count == 0
    assert result_b.error_count == 6
    assert result_b.latencies_ms == []


def test_run_simultaneous_handles_connection_error():
    result_a, result_b = run_simultaneous(
        "http://127.0.0.1:1/",
        "http://127.0.0.1:2/",
        requests=4,
        concurrency=2,
        timeout=1.0,
    )
    assert result_a.error_count == 4
    assert result_b.error_count == 4
    assert all(r.error is not None for r in result_a.results + result_b.results)


def test_run_simultaneous_rejects_zero_concurrency(fast_server):
    with pytest.raises(ValueError):
        run_simultaneous(fast_server, fast_server, requests=5, concurrency=0)


def test_run_simultaneous_warmup_is_untimed_and_not_counted(tracked_server_pair):
    url_a, url_b, tracker = tracked_server_pair(delay_seconds=0.02)
    result_a, result_b = run_simultaneous(
        url_a, url_b, requests=8, concurrency=4, warmup=4
    )
    assert tracker.counts["A"] == 12
    assert tracker.counts["B"] == 12
    assert len(result_a.results) == 8
    assert len(result_b.results) == 8


def test_run_simultaneous_warmup_runs_in_parallel(tracked_server_pair):
    # Warm-up waves use up to `concurrency` requests at once, so every pool
    # connection gets opened before measuring starts.
    url_a, url_b, tracker = tracked_server_pair(delay_seconds=0.1)
    run_simultaneous(url_a, url_b, requests=1, concurrency=1, warmup=0)
    assert tracker.peak["A"] == 1
    url_a, url_b, tracker = tracked_server_pair(delay_seconds=0.1)
    run_simultaneous(url_a, url_b, requests=4, concurrency=4, warmup=4)
    assert tracker.peak["A"] == 4
    assert tracker.counts["A"] == 8


# --------------------------------------------------------------------------
# Cross-mode consistency: "--requests N" means the same thing in both modes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "runner,kwargs",
    [
        (run_interleaved, {}),
        (run_simultaneous, {"concurrency": 1}),
        (run_simultaneous, {"concurrency": 7}),
        (run_simultaneous, {"concurrency": 15}),
    ],
    ids=["interleaved", "simultaneous-c1", "simultaneous-c7", "simultaneous-c15"],
)
def test_requests_means_n_per_target_in_every_mode(tracked_server_pair, runner, kwargs):
    url_a, url_b, tracker = tracked_server_pair(delay_seconds=0.005)
    result_a, result_b = runner(url_a, url_b, requests=15, **kwargs)
    assert tracker.counts["A"] == 15
    assert tracker.counts["B"] == 15
    assert len(result_a.results) == len(result_b.results) == 15

