# ⚡ DiffBench

> Fast, side-by-side performance benchmarking and semantic payload verification for backend APIs.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

DiffBench is a CLI utility and CI gatekeeper built to answer two critical questions when refactoring or migrating an endpoint:
1. **Is the new implementation actually faster?** (Throughput, $p50$, $p90$, $p99$ tail latency)
2. **Did the refactor break the contract?** (Semantic JSON response verification)

---

## 🚧 Project Status

DiffBench is under active early development. Everything below describes the
full intended vision; only the pieces marked **(implemented)** exist today.

- ✅ **(implemented)** `diffbench compare URL_A URL_B` with two modes:
  - **Interleaved** (default): one request at a time, alternating targets in
    ABBA order (A B, B A, ...). Safe when A and B share a database.
  - **Simultaneous**: loads A and B at the same time with `--concurrency`
    requests in flight **per target**. Passing `-c` above 1 selects it
    automatically. Also reports per-target Duration and Throughput.
  - Percentiles: p50, p90, p95, p99 (plus min/mean/max).
  - **Noise-aware verdict**: compares p50 and only declares a winner when the
    difference is beyond run-to-run noise (see below).
  - Warm-up: untimed requests before measuring (`--warmup`, default one per
    connection), excluded from all stats.
  - Exits with code 1 if either target had no successful requests.
  - Common flags: `--requests`, `--method`, `--header`, `--data`.
- ⬜ (planned) Request-rate cap (`--rate`)
- ⬜ (planned) Semantic JSON payload verification (`--verify-json`, `--ignore-keys`)
- ⬜ (planned) CI regression gating (`--max-regression-p99`, `--output-markdown`)
- ⬜ (planned) PyPI package (`pip install diffbench`)

> **Only benchmark systems you own or have permission to test.** Simultaneous
> mode in particular can put real load on a server.

### How `--requests` and `--concurrency` work

- `--requests N` = exactly **N measured requests to Target A and N to Target
  B**, in every mode. The mode only changes *when* they happen.
- `--concurrency C` = how many requests are **in flight at once, per
  target**. `-n 1000 -c 20` means 1,000 samples per target, collected at a
  steady 20 at a time. If C is larger than N, it is capped to N.
- Default `-n` is 100. Warm-up requests are extra and never counted in N.

| Command | Mode | Requests per target | In flight per target |
|---|---|---|---|
| `compare A B` | interleaved | 100 | 1 (alternating A/B) |
| `compare A B -c 25` | simultaneous (auto) | 100 | 25 |
| `compare A B --mode simultaneous` | simultaneous | 100 | 10 (default) |
| `compare A B --mode interleaved -c 25` | error: contradictory flags | | |

> ⚠️ Latencies from the two modes measure different conditions (no load vs.
> concurrent load). Compare A against B **within** one run; don't compare an
> interleaved run's numbers with a simultaneous run's.

### How the verdict avoids reporting noise as a result

Every request takes a slightly different time, so two runs of the *same*
server never match exactly. DiffBench uses a **bootstrap** test: it re-samples
the collected latencies thousands of times to estimate how much p50 could move
by chance, giving a 95% confidence interval (CI) for the change from A to B.

- CI entirely below/above 0 and the change is at least 2%: **faster/slower**.
- CI includes 0: **no clear difference**. Within noise at this sample size.
- Beyond noise but under 2%: **no meaningful difference**.

Each percentile is only judged once there are enough samples to estimate it:
20+ per target for p50, 100+ for p90, 200+ for p95, 1000+ for p99. Below that,
the table shows the change but labels it `too few samples`.

What the test can't catch: differences caused by the setup rather than the
code, such as A on localhost and B on a remote server, or one response cached
and the other not. Make sure both targets run in comparable environments.

To try it today, clone the repo and install it locally in editable mode:

```bash
git clone https://github.com/BD-S78/Diffbench.git
cd Diffbench
python -m venv .venv && .venv\Scripts\activate  # or `source .venv/bin/activate` on macOS/Linux
pip install -e .
diffbench compare http://localhost:8000/api/users http://localhost:8080/api/users
diffbench compare http://localhost:8000/api/users http://localhost:8080/api/users --requests 500 --concurrency 25
```

---

## Roadmap: Full Vision

> Everything from here down describes where DiffBench is headed. Flags and
> commands shown that aren't listed as implemented above (e.g.
> `--verify-json`, `--ignore-keys`, `--max-regression-p99`, `--output-markdown`,
> `pip install diffbench`) are not yet available — see **Project Status** above.
> The sample output below is illustrative; the real table also has Min, Mean,
> p90 and Max rows, and a verdict line with a confidence interval.

## Features

- **Side-by-Side Analysis:** Tests Baseline (Target A) and Candidate (Target B) side-by-side.
- **Statistical Tail Latencies:** Tracks accurate percentiles ($p50$, $p90$, $p95$, $p99$) rather than skewed averages.
- **Semantic Payload Diffing:** Compares response structures and data types while ignoring dynamic noise like timestamps or UUIDs.
- **Contention Prevention:** Supports both simultaneous concurrent stress runs and interleaved (round-robin) modes for APIs sharing an underlying database.
- **CI/CD Ready:** Fail pull requests on latency regressions or contract drift using custom thresholds and Markdown summaries.

---

## Installation

```bash
pip install diffbench
```

Or run directly without installing:

```bash
pipx run diffbench --help
```

---

## Quickstart

### 1. Basic Comparison
Compare two live versions of an endpoint:

```bash
diffbench compare http://localhost:8000/api/users http://localhost:8080/api/users \
  --requests 500 \
  --concurrency 25
```

### 2. Testing Authenticated POST Endpoints
```bash
diffbench compare http://localhost:8000/checkout http://localhost:8080/checkout \
  --method POST \
  --header "Authorization: Bearer <TOKEN>" \
  --header "Content-Type: application/json" \
  --data '{"cart_id": "1001"}'
```

### 3. Semantic Verification with Dynamic Keys Ignored
Verify response integrity while ignoring generated fields:

```bash
diffbench compare http://localhost:8000/v1/profile http://localhost:8000/v2/profile \
  --verify-json \
  --ignore-keys "timestamp,request_id,last_active"
```

---

## Sample Output

```text
┌─────────────────────────┬──────────────┬──────────────┬──────────────────┐
│ Metric                  │ Target A     │ Target B     │ Delta            │
├─────────────────────────┼──────────────┼──────────────┼──────────────────┤
│ Total Requests          │ 1000         │ 1000         │ 0                │
│ Successful (2xx)        │ 1000 (100%)  │ 1000 (100%)  │ 0.0%             │
│ Errors (4xx/5xx)        │ 0            │ 0            │ 0                │
│ Throughput              │ 312.4 req/s  │ 845.1 req/s  │ +170.5% (Faster) │
│ p50 Latency             │ 3.12 ms      │ 1.15 ms      │ -63.1% (Faster)  │
│ p95 Latency             │ 8.45 ms      │ 2.40 ms      │ -71.6% (Faster)  │
│ p99 Latency (Tail)      │ 15.20 ms     │ 4.10 ms      │ -73.0% (Faster)  │
│ Payload Verification    │ PASS         │ PASS         │ 0 mismatches     │
└─────────────────────────┴──────────────┴──────────────┴──────────────────┘
```

---

## CI/CD Integration (GitHub Actions)

Add DiffBench to pull request pipelines to automatically catch performance regressions:

```yaml
name: Performance Check

on: [pull_request]

jobs:
  benchmark:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install DiffBench
        run: pip install diffbench

      - name: Run Comparison Gate
        run: |
          diffbench compare http://localhost:8000/users http://localhost:8081/users \
            --requests 1000 \
            --concurrency 20 \
            --max-regression-p99 15 \
            --verify-json \
            --output-markdown pr_report.md
```

---

## License

Distributed under the MIT License. See `LICENSE` for more information.