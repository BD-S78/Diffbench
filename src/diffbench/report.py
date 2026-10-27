"""Renders the comparison header, table, warnings and verdict."""

from __future__ import annotations

from typing import Optional

from diffbench.significance import Outcome, PercentileComparison
from diffbench.stats import Stats

_COLUMNS = ("Metric", "Target A", "Target B", "Delta")

# Below this many samples per target, tail percentiles (p95/p99) are rough.
SMALL_SAMPLE_THRESHOLD = 100

_TABLE_LABEL = {
    Outcome.FASTER: "Faster",
    Outcome.SLOWER: "Slower",
    Outcome.NEGLIGIBLE: "negligible",
    Outcome.NO_CLEAR_DIFFERENCE: "within noise",
    Outcome.TOO_FEW_SAMPLES: "too few samples",
}


def _fmt_ms(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f} ms"


def _fmt_s(value: float) -> str:
    return f"{value:.2f} s"


def _fmt_rps(value: float) -> str:
    return f"{value:.1f} req/s"


def _fmt_pct(value: float) -> str:
    return f"{value:+.1f}%"


def _change(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or a == 0:
        return None
    return (b - a) / a * 100


def _plain_delta(a: Optional[float], b: Optional[float]) -> str:
    """Percent change with no Faster/Slower claim (not noise-tested)."""
    pct = _change(a, b)
    return "n/a" if pct is None else _fmt_pct(pct)


def _directional_delta(a: float, b: float, *, higher_is_better: bool) -> str:
    """Percent change labelled Faster/Slower (single whole-run measurements)."""
    pct = _change(a, b)
    if pct is None:
        return "n/a"
    if pct == 0:
        return f"{_fmt_pct(pct)} (Same)"
    improved = pct > 0 if higher_is_better else pct < 0
    return f"{_fmt_pct(pct)} ({'Faster' if improved else 'Slower'})"


def _tested_delta(comparison: Optional[PercentileComparison]) -> str:
    """Percent change labelled by the noise test's outcome."""
    if comparison is None or comparison.pct_change is None:
        return "n/a"
    label = _TABLE_LABEL.get(comparison.outcome)
    pct = _fmt_pct(comparison.pct_change)
    return f"{pct} ({label})" if label else pct


def render_header(
    mode: str, requests: int, concurrency: int, warmup: int = 0
) -> str:
    """Describe how the numbers were produced, so runs aren't mis-compared.

    Latencies from interleaved and simultaneous runs measure different
    conditions (no load vs. concurrent load) and should not be compared
    with each other directly.
    """
    header = (
        f"Mode: {mode} | {requests} request(s) per target | "
        f"concurrency {concurrency} per target"
    )
    if warmup:
        header += f" | warmup {warmup} untimed per target"
    return header


def _rows(
    stats_a: Stats,
    stats_b: Stats,
    comparisons: dict[int, PercentileComparison],
    *,
    include_throughput: bool,
) -> list[tuple[str, str, str, str]]:
    def counts(label: str, a: int, b: int) -> tuple[str, str, str, str]:
        return (label, str(a), str(b), f"{b - a:+d}" if b != a else "0")

    def tested(label: str, q: int, a: Optional[float], b: Optional[float]):
        return (label, _fmt_ms(a), _fmt_ms(b), _tested_delta(comparisons.get(q)))

    rows = [
        counts("Total Requests", stats_a.count, stats_b.count),
        counts("Successful", stats_a.success_count, stats_b.success_count),
        counts("Errors", stats_a.error_count, stats_b.error_count),
        # Min/Mean/Max are not noise-tested (min/max are single samples and
        # the mean is outlier-sensitive), so they carry no Faster/Slower tag.
        ("Min Latency", _fmt_ms(stats_a.min_ms), _fmt_ms(stats_b.min_ms),
         _plain_delta(stats_a.min_ms, stats_b.min_ms)),
        ("Mean Latency", _fmt_ms(stats_a.mean_ms), _fmt_ms(stats_b.mean_ms),
         _plain_delta(stats_a.mean_ms, stats_b.mean_ms)),
        tested("p50 Latency", 50, stats_a.p50_ms, stats_b.p50_ms),
        tested("p90 Latency", 90, stats_a.p90_ms, stats_b.p90_ms),
        tested("p95 Latency", 95, stats_a.p95_ms, stats_b.p95_ms),
        tested("p99 Latency (Tail)", 99, stats_a.p99_ms, stats_b.p99_ms),
        ("Max Latency", _fmt_ms(stats_a.max_ms), _fmt_ms(stats_b.max_ms),
         _plain_delta(stats_a.max_ms, stats_b.max_ms)),
    ]
    if include_throughput:
        rows += [
            ("Duration", _fmt_s(stats_a.duration_s), _fmt_s(stats_b.duration_s),
             _directional_delta(stats_a.duration_s, stats_b.duration_s,
                                higher_is_better=False)),
            ("Throughput", _fmt_rps(stats_a.throughput_rps),
             _fmt_rps(stats_b.throughput_rps),
             _directional_delta(stats_a.throughput_rps, stats_b.throughput_rps,
                                higher_is_better=True)),
        ]
    return rows


def render_table(
    stats_a: Stats,
    stats_b: Stats,
    comparisons: Optional[dict[int, PercentileComparison]] = None,
    *,
    include_throughput: bool = False,
) -> str:
    """Render a plain-text side-by-side comparison table.

    Percentile rows are labelled by the noise test in `comparisons`
    (Faster / Slower / negligible / within noise / too few samples).
    `include_throughput` adds per-target Duration and Throughput rows, which
    are only meaningful in simultaneous mode (each target on its own clock).
    """
    body = _rows(stats_a, stats_b, comparisons or {}, include_throughput=include_throughput)
    rows = [_COLUMNS, *body]
    widths = [max(len(row[i]) for row in rows) for i in range(len(_COLUMNS))]

    def _line() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def _row(values: tuple[str, ...]) -> str:
        cells = [f" {v.ljust(widths[i])} " for i, v in enumerate(values)]
        return "|" + "|".join(cells) + "|"

    lines = [_line(), _row(_COLUMNS), _line()]
    lines += [_row(row) for row in body]
    lines.append(_line())
    return "\n".join(lines)


def has_verdict_data(stats_a: Stats, stats_b: Stats) -> bool:
    """True if both targets have at least one successful request."""
    return stats_a.has_latency_data and stats_b.has_latency_data


def render_warnings(stats_a: Stats, stats_b: Stats, requests: int) -> list[str]:
    """Data-quality warnings to show before the verdict."""
    warnings: list[str] = []
    for name, stats in (("Target A", stats_a), ("Target B", stats_b)):
        if stats.error_count:
            warnings.append(
                f"Warning: {name}: {stats.error_count}/{stats.count} requests failed "
                f"({stats.error_rate:.0%}). Latency stats use successful requests only."
            )
    if requests < SMALL_SAMPLE_THRESHOLD:
        warnings.append(
            f"Note: {requests} request(s) per target is a small sample. Tail "
            f"percentiles (p95/p99) are rough at this size; use -n "
            f"{SMALL_SAMPLE_THRESHOLD} or more for p90, 1000+ to compare p99."
        )
    return warnings


def _ci_text(c: PercentileComparison) -> str:
    return f"95% CI {_fmt_pct(c.ci_low_pct)} to {_fmt_pct(c.ci_high_pct)}"


def _tail_text(c: Optional[PercentileComparison]) -> str:
    """Short description of the p99 comparison, appended to the verdict."""
    if c is None:
        return ""
    if c.outcome is Outcome.TOO_FEW_SAMPLES:
        return f"p99 not compared (needs {c.min_samples}+ samples per target)."
    if c.pct_change is None:
        return ""
    labels = {
        Outcome.FASTER: "faster",
        Outcome.SLOWER: "slower",
        Outcome.NEGLIGIBLE: "negligible",
        Outcome.NO_CLEAR_DIFFERENCE: "within noise",
    }
    return f"p99 {_fmt_pct(c.pct_change)} ({labels.get(c.outcome, 'n/a')})."


def render_verdict(
    stats_a: Stats,
    stats_b: Stats,
    comparisons: dict[int, PercentileComparison],
) -> str:
    """Plain-language verdict, based on p50 and decided by the noise test.

    p50 is used rather than the mean so a single slow outlier can't swing
    the verdict; p99 is reported alongside it.
    """
    if not has_verdict_data(stats_a, stats_b):
        failed = [
            name
            for name, s in (("Target A", stats_a), ("Target B", stats_b))
            if not s.has_latency_data
        ]
        return (
            f"Verdict: No speed comparison possible: {' and '.join(failed)} had no "
            f"successful requests. Check the URL(s) and that the server(s) are up."
        )

    p50 = comparisons[50]
    tail = _tail_text(comparisons.get(99))

    if p50.outcome is Outcome.TOO_FEW_SAMPLES:
        body = (
            f"Not enough data for a verdict: p50 {_fmt_pct(p50.pct_change)} observed, "
            f"but {p50.min_samples}+ samples per target are needed to separate a "
            f"real difference from noise."
        )
    elif p50.outcome in (Outcome.FASTER, Outcome.SLOWER):
        body = (
            f"Target B is {p50.outcome.value} than Target A: p50 "
            f"{_fmt_pct(p50.pct_change)} ({_ci_text(p50)})."
        )
    elif p50.outcome is Outcome.NEGLIGIBLE:
        body = (
            f"No meaningful difference: p50 {_fmt_pct(p50.pct_change)} "
            f"({_ci_text(p50)}). Measurable, but too small to matter."
        )
    elif p50.pct_change is None:
        body = "No clear difference (baseline latency too small to compare)."
    else:
        body = (
            f"No clear difference: p50 {_fmt_pct(p50.pct_change)} ({_ci_text(p50)}). "
            f"Within run-to-run noise at this sample size."
        )
    return f"Verdict: {body}" + (f" {tail}" if tail else "")
