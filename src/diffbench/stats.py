"""Aggregates raw latency samples into summary statistics."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional, Sequence

# Percentiles reported in the comparison table.
REPORTED_PERCENTILES = (50, 90, 95, 99)


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Return the q-th percentile (0-100) of already-sorted values.

    Uses linear interpolation between the two nearest ranks -- the same
    definition as numpy's default (``method="linear"``) and Python's
    ``statistics.quantiles(method="inclusive")``. Implemented by hand so it
    works for a single sample on every supported Python version and can be
    reused cheaply inside the bootstrap loop.
    """
    n = len(sorted_values)
    if n == 0:
        raise ValueError("percentile() requires at least one value")
    if not 0 <= q <= 100:
        raise ValueError("q must be between 0 and 100")
    if n == 1:
        return float(sorted_values[0])
    pos = (n - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def min_samples_for_percentile(q: float) -> int:
    """Samples per target needed before a q-th percentile can be compared.

    Rule of thumb: you want roughly 10 observations *beyond* the percentile
    for its estimate to be stable. p50 -> 20, p90 -> 100, p95 -> 200,
    p99 -> 1000.
    """
    tail = min(q, 100 - q) / 100
    if tail <= 0:
        raise ValueError("q must be strictly between 0 and 100")
    return math.ceil(round(10 / tail, 9))


@dataclass
class Stats:
    """Summary statistics for a set of latency samples (milliseconds).

    Latency fields are ``None`` when there were no successful requests, so
    a fully failing target can never masquerade as a 0 ms (i.e. "fast") one.
    """

    count: int
    success_count: int
    error_count: int
    min_ms: Optional[float]
    mean_ms: Optional[float]
    p50_ms: Optional[float]
    p90_ms: Optional[float]
    p95_ms: Optional[float]
    p99_ms: Optional[float]
    max_ms: Optional[float]
    duration_s: float = 0.0
    throughput_rps: float = 0.0

    @property
    def has_latency_data(self) -> bool:
        return self.success_count > 0 and self.p50_ms is not None

    @property
    def error_rate(self) -> float:
        return self.error_count / self.count if self.count else 0.0

    @classmethod
    def from_latencies(
        cls,
        latencies_ms: list[float],
        *,
        success_count: int,
        error_count: int,
        duration_s: float = 0.0,
    ) -> "Stats":
        """Build a Stats object from a list of successful-request latencies.

        `success_count`/`error_count` are passed separately because
        `latencies_ms` only ever contains timings for successful requests.
        `throughput_rps` is successful requests per second of `duration_s`
        (0.0 when no duration was measured).
        """
        total = success_count + error_count
        throughput = success_count / duration_s if duration_s > 0 else 0.0

        if not latencies_ms:
            return cls(
                count=total,
                success_count=success_count,
                error_count=error_count,
                min_ms=None,
                mean_ms=None,
                p50_ms=None,
                p90_ms=None,
                p95_ms=None,
                p99_ms=None,
                max_ms=None,
                duration_s=duration_s,
                throughput_rps=throughput,
            )

        ordered = sorted(latencies_ms)
        return cls(
            count=total,
            success_count=success_count,
            error_count=error_count,
            min_ms=ordered[0],
            mean_ms=statistics.fmean(ordered),
            p50_ms=percentile(ordered, 50),
            p90_ms=percentile(ordered, 90),
            p95_ms=percentile(ordered, 95),
            p99_ms=percentile(ordered, 99),
            max_ms=ordered[-1],
            duration_s=duration_s,
            throughput_rps=throughput,
        )
