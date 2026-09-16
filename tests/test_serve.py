"""Tests for the local viewer server (web/serve.py).

Stdlib only, loopback only — no node, no network, no swarmlite. What is
covered is the part holdings owns: a consistent WAL-free snapshot, and the
one thing `http.server` does not do.
"""

from __future__ import annotations

import http.client
import sqlite3
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))
import serve  # noqa: E402


@pytest.fixture
def served(tmp_path):
    """A directory served by the Range handler on an ephemeral port."""
    root = tmp_path / "site"
    root.mkdir()
    (root / "data.bin").write_bytes(bytes(range(256)) * 4)      # 1024 bytes

    class Handler(serve.RangeHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], root
    httpd.shutdown()


def get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), body


# ------------------------------------------------------------ range reads

def test_a_plain_request_still_returns_the_whole_file(served):
    port, _ = served
    status, _, body = get(port, "/data.bin")
    assert status == 200 and len(body) == 1024


def test_a_range_request_returns_only_that_range(served):
    """stdlib http.server ignores Range and answers 200 with everything
    (measured). The viewer's whole economy is ranged reads, so without this
    opening the page downloads the catalog, and local is worse than
    published."""
    port, _ = served
    status, headers, body = get(port, "/data.bin", {"Range": "bytes=10-19"})
    assert status == 206
    assert headers["Content-Range"] == "bytes 10-19/1024"
    assert headers["Content-Length"] == "10"
    assert body == bytes(range(10, 20))


def test_an_open_ended_range_runs_to_the_end(served):
    port, _ = served
    status, headers, body = get(port, "/data.bin", {"Range": "bytes=1020-"})
    assert status == 206 and len(body) == 4
    assert headers["Content-Range"] == "bytes 1020-1023/1024"


def test_a_suffix_range_returns_the_last_bytes(served):
    port, _ = served
    status, headers, body = get(port, "/data.bin", {"Range": "bytes=-4"})
    assert status == 206 and len(body) == 4
    assert headers["Content-Range"] == "bytes 1020-1023/1024"


def test_a_range_past_the_end_is_refused(served):
    port, _ = served
    status, headers, _ = get(port, "/data.bin", {"Range": "bytes=5000-6000"})
    assert status == 416
    assert headers["Content-Range"] == "bytes */1024"


def test_a_malformed_range_falls_back_to_the_whole_file(served):
    port, _ = served
    status, _, body = get(port, "/data.bin", {"Range": "pages=1-2"})
    assert status == 200 and len(body) == 1024


# ------------------------------------------------------------- snapshots

def test_the_snapshot_is_readable_and_complete(tmp_path):
    src = tmp_path / "c.sqlite"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (a)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    conn.commit()
    conn.close()

    out = tmp_path / "snap.sqlite"
    serve.snapshot(str(src), out)
    assert sqlite3.connect(out).execute(
        "SELECT COUNT(*) FROM t").fetchone()[0] == 100


def test_the_snapshot_folds_in_an_open_wal(tmp_path):
    """The reason it is a snapshot at all: the reader cannot see a WAL
    sidecar, so serving a live catalog would read it stale with no warning
    (measured against real swarmlite)."""
    src = tmp_path / "c.sqlite"
    writer = sqlite3.connect(src)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (a)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()                    # left in the WAL, writer still open

    out = tmp_path / "snap.sqlite"
    serve.snapshot(str(src), out)
    try:
        assert sqlite3.connect(out).execute(
            "SELECT COUNT(*) FROM t").fetchone()[0] == 1
        assert not Path(str(out) + "-wal").exists()
        assert sqlite3.connect(out).execute(
            "PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        writer.close()


def test_the_snapshot_does_not_touch_the_original(tmp_path):
    """Observer, not authority — even here."""
    src = tmp_path / "c.sqlite"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (a)")
    conn.commit()
    conn.close()
    before = src.read_bytes()
    serve.snapshot(str(src), tmp_path / "snap.sqlite")
    assert src.read_bytes() == before
