"""Fires HTTP requests against two targets and records timing data.

Two scheduling modes are supported. In both, ``requests`` means exactly that
many measured requests to Target A *and* exactly that many to Target B --
only the timing of those requests differs between modes:

* **Interleaved** (:func:`run_interleaved`): one request in flight at a
  time, alternating targets in ABBA order (A B, B A, A B, ...). Both targets
  are measured across the same wall-clock window (no time-bucket bias),
  neither always goes first (no shared-cache order bias), and A and B are
  never hit at the same instant, so no artificial lock contention is created
  if they share infrastructure such as a database.

* **Simultaneous** (:func:`run_simultaneous`): both targets are loaded at
  the same time, each with ``concurrency`` requests in flight (a rolling
  pool: as one request finishes, the next starts, until the target's
  ``requests`` are done). This measures behaviour under concurrent load.

Each target always gets its own ``httpx.AsyncClient`` with a connection pool
sized to that target's concurrency, so time spent waiting for a free
client-side connection never leaks into the measured server latency.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class RequestResult:
    """Outcome of a single HTTP request."""

    latency_ms: float
    status_code: int | None
    success: bool
    error: str | None = None


@dataclass
class TargetResult:
    """All results collected for one target URL."""

    url: str
    results: list[RequestResult] = field(default_factory=list)
    # Wall-clock seconds from this target's first request starting to its
    # last request finishing. Used for throughput. In interleaved mode both
    # targets share one window, so both get the same (total) duration.
    duration_s: float = 0.0

    @property
    def latencies_ms(self) -> list[float]:
        """Latencies for successful requests only (used for speed stats)."""
        return [r.latency_ms for r in self.results if r.success]

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if not r.success)


_SSL_CONTEXT: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """Build the TLS context once per process and share it.

    Loading the CA bundle is the slow part of creating an httpx client
    (~0.5s on some machines); sharing one context cuts per-client setup to
    ~0.1s. Verification behaviour is identical to httpx's default.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = httpx.create_ssl_context()
    return _SSL_CONTEXT


def _make_client(*, concurrency: int, timeout: float) -> httpx.AsyncClient:
    """Create a per-target client whose pool fits exactly `concurrency`."""
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    return httpx.AsyncClient(timeout=timeout, limits=limits, verify=_ssl_context())


def abba_order(round_index: int) -> tuple[int, int]:
    """Which target goes first in a round: A B, B A, A B, B A, ...

    Alternating who goes first means neither target systematically benefits
    from the other having just warmed a shared cache (e.g. the same database
    rows). Returns indices into (A, B). Deterministic, so tests can assert it.
    """
    return (0, 1) if round_index % 2 == 0 else (1, 0)


async def _fire_one(
    client: httpx.AsyncClient,
    url: str,
    *,
    method: str,
    headers: dict[str, str] | None,
    data: str | None,
) -> RequestResult:
    """Fire a single HTTP request and time it, recording success or failure.

    This is the single timing code path shared by every mode, so differences
    between modes reflect scheduling only, never different client code.
    """
    start = time.perf_counter()
    try:
        response = await client.request(method, url, headers=headers, content=data)
        elapsed_ms = (time.perf_counter() - start) * 1000
        success = 200 <= response.status_code < 400
        return RequestResult(
            latency_ms=elapsed_ms,
            status_code=response.status_code,
            success=success,
        )
    except httpx.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RequestResult(
            latency_ms=elapsed_ms,
            status_code=None,
            success=False,
            error=str(exc),
        )


async def run_interleaved(
    url_a: str,
    url_b: str,
    *,
    requests: int,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: str | None = None,
    timeout: float = 30.0,
    warmup: int = 0,
) -> tuple[TargetResult, TargetResult]:
    """Fire `requests` measured calls per target, one at a time.

    Exactly one request is in flight at a time, in ABBA order
    (A B, B A, A B, ...; see :func:`abba_order`). Both targets are measured
    across the same wall-clock window, so transient conditions affect both
    roughly equally, and A and B are never hit at the same instant (no
    artificial shared-DB contention).

    `warmup` untimed requests per target are sent first (also ABBA, also one
    at a time) to open connections and warm caches. They are NOT counted in
    `requests` and are excluded from all results.
    """
    result_a = TargetResult(url=url_a)
    result_b = TargetResult(url=url_b)
    urls = (url_a, url_b)
    results = (result_a, result_b)

    async with _make_client(concurrency=1, timeout=timeout) as client_a, _make_client(
        concurrency=1, timeout=timeout
    ) as client_b:
        clients = (client_a, client_b)
        fire = dict(method=method, headers=headers, data=data)

        for round_index in range(warmup):
            for i in abba_order(round_index):
                await _fire_one(clients[i], urls[i], **fire)

        start = time.perf_counter()
        for round_index in range(requests):
            for i in abba_order(round_index):
                results[i].results.append(await _fire_one(clients[i], urls[i], **fire))
        elapsed = time.perf_counter() - start

    # Both targets shared one alternating window, so they share its duration.
    result_a.duration_s = elapsed
    result_b.duration_s = elapsed
    return result_a, result_b


async def _load_target(
    client: httpx.AsyncClient,
    url: str,
    *,
    requests: int,
    concurrency: int,
    method: str,
    headers: dict[str, str] | None,
    data: str | None,
) -> TargetResult:
    """Send exactly `requests` calls to one target, `concurrency` at a time.

    Uses a rolling worker pool: `concurrency` workers each pull the next job
    from a shared budget until all `requests` are claimed, so the target sees
    a steady `concurrency` requests in flight (except while draining the
    final few) and never more than `requests` calls in total.

    The client is created by the caller *before* any target starts, because
    building an `httpx.AsyncClient` does blocking work (loading the SSL
    certificate bundle, ~0.5s+). Done here, one target's client setup would
    freeze the event loop while the other target's requests were in flight,
    inflating the other target's measured latency.
    """
    target = TargetResult(url=url)
    remaining = requests

    async def worker() -> None:
        nonlocal remaining
        # Safe without a lock: asyncio is single-threaded, and there is no
        # `await` between checking and decrementing `remaining`.
        while remaining > 0:
            remaining -= 1
            target.results.append(
                await _fire_one(client, url, method=method, headers=headers, data=data)
            )

    start = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    target.duration_s = time.perf_counter() - start
    return target


async def _warm_target(
    client: httpx.AsyncClient,
    url: str,
    *,
    warmup: int,
    concurrency: int,
    method: str,
    headers: dict[str, str] | None,
    data: str | None,
) -> None:
    """Send `warmup` untimed requests to one target, in concurrent waves.

    Waves of up to `concurrency` requests in parallel, so each wave can open
    a pool connection per worker. Results are discarded.
    """
    sent = 0
    while sent < warmup:
        wave = min(concurrency, warmup - sent)
        await asyncio.gather(
            *(
                _fire_one(client, url, method=method, headers=headers, data=data)
                for _ in range(wave)
            )
        )
        sent += wave


async def run_simultaneous(
    url_a: str,
    url_b: str,
    *,
    requests: int,
    concurrency: int,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: str | None = None,
    timeout: float = 30.0,
    warmup: int = 0,
) -> tuple[TargetResult, TargetResult]:
    """Load A and B at the same time, `concurrency` in flight per target.

    Both targets receive exactly `requests` measured calls each, and their
    worker pools start together, so the targets overlap in time (up to
    2 x `concurrency` requests in flight in total). If one target finishes
    first, the other continues alone; each target's own `duration_s` makes
    that visible in the report rather than hiding it.

    `warmup` untimed requests per target are sent first, for both targets at
    once, and fully complete before measurement begins. They are NOT counted
    in `requests` and are excluded from all results.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    # Never spin up more workers than there are requests to send.
    concurrency = min(concurrency, requests)

    fire = dict(method=method, headers=headers, data=data)
    # Build BOTH clients before either target starts (see _load_target).
    async with _make_client(
        concurrency=concurrency, timeout=timeout
    ) as client_a, _make_client(concurrency=concurrency, timeout=timeout) as client_b:
        if warmup:
            await asyncio.gather(
                _warm_target(client_a, url_a, warmup=warmup, concurrency=concurrency, **fire),
                _warm_target(client_b, url_b, warmup=warmup, concurrency=concurrency, **fire),
            )
        result_a, result_b = await asyncio.gather(
            _load_target(client_a, url_a, requests=requests, concurrency=concurrency, **fire),
            _load_target(client_b, url_b, requests=requests, concurrency=concurrency, **fire),
        )
    return result_a, result_b
