import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _TestHTTPServer(ThreadingHTTPServer):
    # The stdlib default listen backlog is 5; on Windows, connections beyond
    # the backlog are refused rather than queued, which would make
    # concurrency tests flaky. Real servers use much larger backlogs.
    request_queue_size = 128


class HitTracker:
    """Thread-safe record of what the test servers actually experienced.

    Tracks, per server label: total hits and the peak number of requests in
    flight at once, plus the peak in flight across ALL labels combined (used
    to prove two targets were, or were not, being hit at the same time).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: Counter = Counter()
        self._in_flight: Counter = Counter()
        self.peak: Counter = Counter()
        self._total_in_flight = 0
        self.peak_total = 0

    def enter(self, label: str) -> None:
        with self._lock:
            self.counts[label] += 1
            self._in_flight[label] += 1
            self.peak[label] = max(self.peak[label], self._in_flight[label])
            self._total_in_flight += 1
            self.peak_total = max(self.peak_total, self._total_in_flight)

    def exit(self, label: str) -> None:
        with self._lock:
            self._in_flight[label] -= 1
            self._total_in_flight -= 1


class _SlowHandler(BaseHTTPRequestHandler):
    """Test HTTP handler that sleeps a configurable amount before replying."""

    delay_seconds: float = 0.0
    fail: bool = False
    label: str = ""
    hit_log: list | None = None
    tracker: HitTracker | None = None

    def _handle(self):
        if self.hit_log is not None:
            self.hit_log.append(self.label)
        if self.tracker is not None:
            self.tracker.enter(self.label)
        try:
            self._respond()
        finally:
            if self.tracker is not None:
                self.tracker.exit(self.label)

    def _respond(self):
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.fail:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"error")
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def log_message(self, format, *args):  # noqa: A002 - silence test server logs
        pass


def make_server(
    delay_seconds: float = 0.0,
    fail: bool = False,
    label: str = "",
    hit_log: list | None = None,
    tracker: HitTracker | None = None,
):
    """Start a background HTTP server on localhost and return (server, url)."""
    handler_cls = type(
        "ConfiguredSlowHandler",
        (_SlowHandler,),
        {
            "delay_seconds": delay_seconds,
            "fail": fail,
            "label": label,
            "hit_log": hit_log,
            "tracker": tracker,
        },
    )
    server = _TestHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    return server, url


@pytest.fixture
def fast_server():
    server, url = make_server(delay_seconds=0.0)
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def slow_server():
    server, url = make_server(delay_seconds=0.03)
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def failing_server():
    server, url = make_server(fail=True)
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def labeled_server_pair():
    """Two servers (A, B) that both append their label to a shared hit_log
    list on every request, so tests can assert on the actual order requests
    were dispatched in (e.g. to verify interleaving)."""
    hit_log: list = []
    server_a, url_a = make_server(label="A", hit_log=hit_log)
    server_b, url_b = make_server(label="B", hit_log=hit_log)
    yield url_a, url_b, hit_log
    server_a.shutdown()
    server_a.server_close()
    server_b.shutdown()
    server_b.server_close()


@pytest.fixture
def tracked_server_pair():
    """Factory fixture: call with a per-request delay (seconds) to get
    (url_a, url_b, tracker) for two servers sharing one HitTracker.

    A non-trivial delay keeps requests in flight long enough for the
    tracker to observe real overlap reliably.
    """
    servers = []

    def _make(delay_seconds: float = 0.1):
        tracker = HitTracker()
        server_a, url_a = make_server(
            delay_seconds=delay_seconds, label="A", tracker=tracker
        )
        server_b, url_b = make_server(
            delay_seconds=delay_seconds, label="B", tracker=tracker
        )
        servers.extend([server_a, server_b])
        return url_a, url_b, tracker

    yield _make
    for server in servers:
        server.shutdown()
        server.server_close()
