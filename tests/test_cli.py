from typer.testing import CliRunner

from diffbench.cli import app

runner = CliRunner()


def _flat(text: str) -> str:
    """Collapse Rich's boxed, line-wrapped error panels into plain text."""
    for box_char in "│┌┐└┘─":
        text = text.replace(box_char, " ")
    return " ".join(text.split())


def test_compare_runs_end_to_end(fast_server, slow_server):
    result = runner.invoke(app, ["compare", fast_server, slow_server, "--requests", "5"])
    assert result.exit_code == 0, result.output
    assert "Target A" in result.output
    assert "Target B" in result.output
    assert "p50 Latency" in result.output
    assert "p99 Latency (Tail)" in result.output
    assert "Verdict:" in result.output


def test_compare_defaults_to_interleaved_mode(fast_server, slow_server):
    result = runner.invoke(app, ["compare", fast_server, slow_server, "-n", "3"])
    assert result.exit_code == 0, result.output
    assert "Mode: interleaved" in result.output
    assert "concurrency 1 per target" in result.output
    # Default warm-up: one untimed request per connection.
    assert "warmup 1 untimed per target" in result.output
    # Throughput rows are only shown for simultaneous mode.
    assert "Throughput" not in result.output


def test_compare_default_request_count_is_100():
    from diffbench.cli import DEFAULT_REQUESTS

    assert DEFAULT_REQUESTS == 100


def test_compare_detects_a_clearly_faster_candidate(slow_server, fast_server):
    # A = 30ms server, B = ~0ms server: a real, large difference.
    result = runner.invoke(app, ["compare", slow_server, fast_server, "-n", "40"])
    assert result.exit_code == 0, result.output
    assert "Target B is faster than Target A" in result.output


def test_compare_same_server_twice_is_not_called_faster(slow_server):
    # The core guarantee against noise: a server compared with itself.
    result = runner.invoke(app, ["compare", slow_server, slow_server, "-n", "40"])
    assert result.exit_code == 0, result.output
    assert "faster than" not in result.output
    assert "slower than" not in result.output


def test_compare_exits_1_when_candidate_always_fails(fast_server, failing_server):
    result = runner.invoke(app, ["compare", fast_server, failing_server, "-n", "5"])
    assert result.exit_code == 1, result.output
    assert "No speed comparison possible" in result.output
    assert "Target B is faster" not in result.output
    assert "Target B: 5/5 requests failed" in result.output


def test_compare_warns_about_small_samples(fast_server, slow_server):
    result = runner.invoke(app, ["compare", fast_server, slow_server, "-n", "5"])
    assert "small sample" in result.output


def test_compare_custom_warmup(fast_server, slow_server):
    result = runner.invoke(app, ["compare", fast_server, slow_server, "-n", "3", "-w", "0"])
    assert result.exit_code == 0, result.output
    assert "warmup" not in result.output.splitlines()[0]


def test_compare_rejects_negative_warmup(fast_server, slow_server):
    result = runner.invoke(app, ["compare", fast_server, slow_server, "-w", "-1"])
    assert result.exit_code != 0


def test_concurrency_alone_selects_simultaneous_mode(fast_server, slow_server):
    result = runner.invoke(app, ["compare", fast_server, slow_server, "-n", "6", "-c", "3"])
    assert result.exit_code == 0, result.output
    assert "Mode: simultaneous" in result.output
    assert "concurrency 3 per target" in result.output
    assert "warmup 3 untimed per target" in result.output


def test_compare_simultaneous_mode(fast_server, slow_server):
    result = runner.invoke(
        app,
        ["compare", fast_server, slow_server, "-n", "6", "--mode", "simultaneous", "-c", "3"],
    )
    assert result.exit_code == 0, result.output
    assert "Mode: simultaneous" in result.output
    assert "6 request(s) per target" in result.output
    assert "concurrency 3 per target" in result.output
    assert "Throughput" in result.output
    assert "Duration" in result.output


def test_compare_simultaneous_defaults_concurrency_to_10(fast_server, slow_server):
    result = runner.invoke(
        app, ["compare", fast_server, slow_server, "-n", "20", "--mode", "simultaneous"]
    )
    assert result.exit_code == 0, result.output
    assert "concurrency 10 per target" in result.output


def test_compare_simultaneous_caps_concurrency_with_note(fast_server, slow_server):
    result = runner.invoke(
        app,
        ["compare", fast_server, slow_server, "-n", "4", "--mode", "simultaneous", "-c", "50"],
    )
    assert result.exit_code == 0, result.output
    assert "concurrency 4 per target" in result.output
    assert "using concurrency 4 instead" in result.stderr


def test_explicit_interleaved_with_concurrency_is_rejected(fast_server, slow_server):
    result = runner.invoke(
        app,
        ["compare", fast_server, slow_server, "-n", "3", "--mode", "interleaved", "-c", "5"],
    )
    assert result.exit_code != 0
    assert "contradicts --concurrency 5" in _flat(result.output)


def test_concurrency_1_without_mode_stays_interleaved(fast_server, slow_server):
    # -c 1 reads as "one at a time", so it keeps the default interleaved mode.
    result = runner.invoke(
        app, ["compare", fast_server, slow_server, "-n", "2", "-c", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "Mode: interleaved" in result.output


def test_explicit_simultaneous_with_concurrency_1(fast_server, slow_server):
    # An explicit mode always wins over the -c heuristic.
    result = runner.invoke(
        app,
        ["compare", fast_server, slow_server, "-n", "2", "--mode", "simultaneous", "-c", "1"],
    )
    assert result.exit_code == 0, result.output
    assert "Mode: simultaneous" in result.output
    assert "concurrency 1 per target" in result.output


def test_compare_rejects_zero_concurrency(fast_server, slow_server):
    result = runner.invoke(
        app,
        ["compare", fast_server, slow_server, "-n", "3", "--mode", "simultaneous", "-c", "0"],
    )
    assert result.exit_code != 0
    # Also rejected when no mode is given.
    result = runner.invoke(app, ["compare", fast_server, slow_server, "-n", "3", "-c", "0"])
    assert result.exit_code != 0


def test_compare_rejects_unknown_mode(fast_server, slow_server):
    result = runner.invoke(
        app, ["compare", fast_server, slow_server, "--mode", "blast"]
    )
    assert result.exit_code != 0


def test_compare_rejects_invalid_requests_count(fast_server, slow_server):
    result = runner.invoke(app, ["compare", fast_server, slow_server, "--requests", "0"])
    assert result.exit_code != 0


def test_compare_rejects_malformed_header(fast_server, slow_server):
    result = runner.invoke(
        app,
        ["compare", fast_server, slow_server, "--requests", "1", "--header", "not-a-header"],
    )
    assert result.exit_code != 0
