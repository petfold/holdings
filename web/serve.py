"""Serve the viewer against a local catalog, offline.

    python web/serve.py --db ~/catalog.sqlite

The same page as `web/publish.py` produces, answered from this machine
instead of from Swarm. Two reasons it exists:

  * **Offline.** A published catalog is fetched page by page at query time,
    so it needs connectivity and a gateway. This needs neither, which
    matters for a tool whose job is answering questions about drives you
    are standing in front of with no signal.
  * **No apparatus.** No Bee node, no wallet, no postage batch. Wanting a
    nicer interface than the terminal should not require any of those.

Needs nothing installed beyond the stdlib -- not even swarmlite's Python
package. The reader is JavaScript, so it is copied as files; the catalog
snapshot is taken with sqlite3's own backup API.

The catalog is served as a *snapshot*, not live: SQLite's WAL sidecar is
invisible to the reader (measured -- an uncommitted-to-main write is simply
absent), so a live file would be read stale with no warning. Re-run to pick
up a newer scan.
"""

import argparse
import http.server
import json
import os
import re
import socketserver
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish import copy_page, find_swarmlite_js, write_config  # noqa: E402

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler, plus the one thing it does not do.

    The stdlib server ignores `Range` and answers 200 with the whole file
    (measured). The viewer's whole economy is ranged reads -- a query
    fetches tens of kilobytes of a catalog that may be hundreds of
    megabytes -- so without this, opening the page downloads everything and
    the local case is worse than the published one.
    """

    def send_head(self):
        header = self.headers.get("Range")
        if not header:
            return super().send_head()
        m = RANGE_RE.match(header.strip())
        path = self.translate_path(self.path)
        if not m or os.path.isdir(path):
            return super().send_head()
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            self.send_error(404)
            return None

        start, end = m.group(1), m.group(2)
        if start == "":                       # bytes=-N: the final N bytes
            length = min(int(end or 0), size)
            first, last = size - length, size - 1
        else:
            first = int(start)
            last = min(int(end), size - 1) if end else size - 1
        if first >= size or first > last:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None

        f.seek(first)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
        self.send_header("Content-Length", str(last - first + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        return _Slice(f, last - first + 1)

    def end_headers(self):
        # A published root serves the page and the catalog from one origin;
        # locally they are one origin too, but say so for anyone pointing
        # the page at a catalog served elsewhere.
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def log_message(self, fmt, *args):
        if os.environ.get("HOLDINGS_SERVE_VERBOSE"):
            super().log_message(fmt, *args)


class _Slice:
    """A file object that stops after `remaining` bytes, for copyfile()."""

    def __init__(self, fileobj, remaining):
        self.f, self.remaining = fileobj, remaining

    def read(self, n=-1):
        if self.remaining <= 0:
            return b""
        if n is None or n < 0:
            n = self.remaining
        data = self.f.read(min(n, self.remaining))
        self.remaining -= len(data)
        return data

    def close(self):
        self.f.close()


def snapshot(db_path: str, out: Path) -> None:
    """A consistent, WAL-free copy of the catalog, using only the stdlib.

    sqlite3's backup API gives a coherent copy of a database someone may be
    writing to; switching the copy to journal_mode=DELETE folds the WAL in,
    which is what makes it readable by a VFS that cannot see the sidecar.
    """
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(out))
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.commit()
    finally:
        dst.close()
        src.close()
    for sidecar in ("-wal", "-shm"):
        Path(str(out) + sidecar).unlink(missing_ok=True)


def main() -> int:
    default_db = os.environ.get(
        "HOLDINGS_DB",
        os.path.join(os.path.expanduser("~"), ".local", "share", "holdings",
                     "catalog.sqlite"))
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=default_db,
                    help=f"catalog to serve (default {default_db})")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="default 127.0.0.1: this machine only")
    ap.add_argument("--label", default="local catalog")
    ap.add_argument("--max-scan-age", type=float, default=30.0, metavar="DAYS")
    ap.add_argument("--swarmlite-js", help="path to swarmlite's js/ directory")
    ap.add_argument("--out", help="assemble here and do not serve")
    args = ap.parse_args()

    if not Path(args.db).is_file():
        sys.exit(f"no catalog at {args.db} -- scan something first, or pass"
                 f" --db")
    js = find_swarmlite_js(args.swarmlite_js)

    def build(site: Path) -> None:
        copy_page(site, js)
        snapshot(args.db, site / "catalog.sqlite")
        write_config(site, {"name": "catalog.sqlite", "label": args.label,
                            "verify": False,
                            "maxScanAgeDays": args.max_scan_age})

    if args.out:
        build(Path(args.out))
        print(f"assembled at {args.out} (not served)")
        return 0

    with tempfile.TemporaryDirectory() as d:
        site = Path(d) / "site"
        build(site)
        size = (site / "catalog.sqlite").stat().st_size

        class Handler(RangeHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(site), **kw)

        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.ThreadingTCPServer((args.host, args.port),
                                             Handler) as httpd:
            print(f"serving a {size / 2 ** 20:.1f} MB snapshot of {args.db}")
            print(f"  http://{args.host}:{httpd.server_address[1]}/")
            print("  (a snapshot, not live -- re-run after the next scan)")
            print("Ctrl-C to stop.")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
