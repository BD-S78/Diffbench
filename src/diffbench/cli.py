"""DiffBench CLI entry point."""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Optional

import typer
from typing_extensions import Annotated

from diffbench.report import (
    has_verdict_data,
    render_header,
    render_table,
    render_verdict,
    render_warnings,
)
from diffbench.runner import TargetResult, run_interleaved, run_simultaneous
from diffbench.significance import compare_percentiles
from diffbench.stats import Stats

DEFAULT_REQUESTS = 100
DEFAULT_SIMULTANEOUS_CONCURRENCY = 10


class Mode(str, Enum):
    """How requests to the two targets are scheduled."""

    interleaved = "interleaved"
    simultaneous = "simultaneous"


def resolve_mode(mode: Optional[Mode], concurrency: Optional[int]) -> tuple[Mode, int]:
    """Decide the effective mode and concurrency from the user's flags.

    * Explicit --mode always wins.
    * No --mode: -c greater than 1 implies simultaneous; otherwise interleaved
      (-c 1 reads as "one at a time").
    * --mode interleaved with -c > 1 is a contradiction and is rejected.
    """
    if concurrency is not None and concurrency < 1:
        raise typer.BadParameter("--concurrency must be at least 1.", param_hint="--concurrency")

    if mode is None:
        mode = (
            Mode.simultaneous
            if concurrency is not None and concurrency > 1
            else Mode.interleaved
        )

    if mode is Mode.interleaved:
        if concurrency is not None and concurrency > 1:
            raise typer.BadParameter(
                "--mode interleaved always sends one request at a time (so A and B "
                "are never hit at the same moment), which contradicts --concurrency "
                f"{concurrency}. Drop --mode to run simultaneously, or drop -c.",
                param_hint="--concurrency",
            )
        return mode, 1

    effective = DEFAULT_SIMULTANEOUS_CONCURRENCY if concurrency is None else concurrency
    return mode, effective

app = typer.Typer(
    name="diffbench",
    help="A/B performance testing and payload regression CLI for backend developers.",
    add_completion=False,
)


@app.callback()
def _callback() -> None:
    """A/B performance testing and payload regression CLI for backend developers."""
    # Presence of this callback forces Typer to keep "compare" as an explicit
    # subcommand name instead of collapsing to a single-command shortcut.


def _parse_headers(header_values: list[str]) -> dict[str, str]:
    """Parse repeated '-H "Key: Value"' flags into a dict."""
    headers: dict[str, str] = {}
    for item in header_values:
        if ":" not in item:
            raise typer.BadParameter(
                f"Header '{item}' must be in 'Key: Value' format."
            )
        key, value = item.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


@app.command()
def compare(
    url_a: Annotated[str, typer.Argument(help="Baseline target URL (Target A).")],
    url_b: Annotated[str, typer.Argument(help="Candidate target URL (Target B).")],
    requests: Annotated[
        int,
        typer.Option(
            "--requests",
            "-n",
            help="Measured requests to send to EACH target (warm-up not included).",
        ),
    ] = DEFAULT_REQUESTS,
    method: Annotated[
        str, typer.Option("--method", "-X", help="HTTP method to use.")
    ] = "GET",
    header: Annotated[
        Optional[list[str]],
        typer.Option("--header", "-H", help='Request header, e.g. "Authorization: Bearer X". Repeatable.'),
    ] = None,
    data: Annotated[
        Optional[str], typer.Option("--data", "-d", help="Request body to send.")
    ] = None,
    mode: Annotated[
        Optional[Mode],
        typer.Option(
            "--mode",
            "-m",
            help=(
                "interleaved: one request at a time, alternating A/B (safe when A "
                "and B share a database). simultaneous: load A and B at the same "
                "time with --concurrency requests in flight per target. Default: "
                "interleaved, or simultaneous if -c is greater than 1."
            ),
            case_sensitive=False,
            show_default=False,
        ),
    ] = None,
    concurrency: Annotated[
        Optional[int],
        typer.Option(
            "--concurrency",
            "-c",
            help=(
                "Requests in flight at once, PER TARGET. A value above 1 selects "
                "simultaneous mode (defaults to "
                f"{DEFAULT_SIMULTANEOUS_CONCURRENCY} in simultaneous mode)."
            ),
            show_default=False,
        ),
    ] = None,
    warmup: Annotated[
        Optional[int],
        typer.Option(
            "--warmup",
            "-w",
            help=(
                "Untimed requests per target sent before measuring, to open "
                "connections and warm caches. Not counted in --requests. Default: "
                "one per connection (1 interleaved, the concurrency simultaneous)."
            ),
            show_default=False,
        ),
    ] = None,
) -> None:
    """Compare speed between two endpoints (Target A = baseline, B = candidate).

    --requests N always means exactly N measured requests to Target A AND
    exactly N to Target B, in every mode. --mode only changes WHEN those
    requests happen, and --concurrency sets how many are in flight at once
    per target.

    The verdict compares p50 latency and only declares a winner when the
    difference is beyond run-to-run noise (95% bootstrap confidence interval).
    Exits with code 1 if either target had no successful requests.

    Only benchmark systems you own or have permission to test.
    """
    if requests < 1:
        raise typer.BadParameter("--requests must be at least 1.", param_hint="--requests")
    if warmup is not None and warmup < 0:
        raise typer.BadParameter("--warmup cannot be negative.", param_hint="--warmup")

    effective_mode, effective_concurrency = resolve_mode(mode, concurrency)
    if effective_concurrency > requests:
        typer.echo(
            f"Note: --concurrency {effective_concurrency} is more than --requests "
            f"{requests}; using concurrency {requests} instead.",
            err=True,
        )
        effective_concurrency = requests
    # Default warm-up: one untimed request per connection that will be used.
    effective_warmup = effective_concurrency if warmup is None else warmup

    headers = _parse_headers(header or [])

    typer.echo(
        render_header(effective_mode.value, requests, effective_concurrency, effective_warmup)
    )
    typer.echo(f"Target A: {url_a}")
    typer.echo(f"Target B: {url_b}")

    common = dict(
        requests=requests,
        method=method,
        headers=headers,
        data=data,
        warmup=effective_warmup,
    )
    if effective_mode is Mode.interleaved:
        result_a, result_b = asyncio.run(run_interleaved(url_a, url_b, **common))
    else:
        result_a, result_b = asyncio.run(
            run_simultaneous(url_a, url_b, concurrency=effective_concurrency, **common)
        )

    stats_a = _to_stats(result_a)
    stats_b = _to_stats(result_b)
    comparisons = compare_percentiles(result_a.latencies_ms, result_b.latencies_ms)

    typer.echo()
    typer.echo(
        render_table(
            stats_a,
            stats_b,
            comparisons,
            include_throughput=effective_mode is Mode.simultaneous,
        )
    )
    typer.echo()
    for warning in render_warnings(stats_a, stats_b, requests):
        typer.echo(warning)
    typer.echo(render_verdict(stats_a, stats_b, comparisons))

    if not has_verdict_data(stats_a, stats_b):
        raise typer.Exit(code=1)


def _to_stats(result: TargetResult) -> Stats:
    return Stats.from_latencies(
        result.latencies_ms,
        success_count=result.success_count,
        error_count=result.error_count,
        duration_s=result.duration_s,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
