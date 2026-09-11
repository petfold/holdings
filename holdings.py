#!/usr/bin/env python3
"""
holdings — a disposable, regenerable catalog of where your files are.

Design principles (see the accompanying README):
  * Observer, not authority: reads filesystems and backup listings,
    never writes to them. Deleting this tool and its database costs
    nothing but convenience — everything is regenerable by re-scanning.
  * Content hash (sha256) is the primary key. Paths, media, and
    categories are attributes of a hash, never identities.
  * Single writer (your backup-node laptop), many readers (the SQLite
    file can be placed in a Syncthing folder and read anywhere).
  * Placement truth lives here, in SQLite. Semantic truth lives in
    OntoDAG. The `project-ontodag` command emits placement facts as a
    namespaced (`sys:`) projection for OntoDAG to ingest as a
    regenerable, non-authoritative view.

Stdlib only. Python 3.9+.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

DEFAULT_DB = os.environ.get(
    "HOLDINGS_DB",
    os.path.join(os.path.expanduser("~"), ".local", "share", "holdings",
                 "catalog.sqlite"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    medium_id     TEXT PRIMARY KEY,          -- e.g. 'drive-budapest', 'laptop-x1', 'restic-b2'
    kind          TEXT NOT NULL,             -- drive | laptop | phone | restic-repo | cloud | other
    location_hint TEXT,                      -- e.g. 'safe, Budapest flat'
    is_backup     INTEGER NOT NULL DEFAULT 0,-- counts toward redundancy as a backup copy
    notes         TEXT,
    last_scanned  REAL
);

CREATE TABLE IF NOT EXISTS content (
    hash       TEXT PRIMARY KEY,             -- 'sha256:...'
    size       INTEGER NOT NULL,
    first_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
    medium_id TEXT NOT NULL REFERENCES media(medium_id),
    path      TEXT NOT NULL,                 -- path relative to the medium's root
    hash      TEXT NOT NULL REFERENCES content(hash),
    size      INTEGER NOT NULL,
    mtime     REAL,                          -- NULL for imported (e.g. restic) listings
    seen_at   REAL NOT NULL,
    PRIMARY KEY (medium_id, path)
);
CREATE INDEX IF NOT EXISTS idx_instances_hash ON instances(hash);

CREATE TABLE IF NOT EXISTS scans (
    scan_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    medium_id  TEXT NOT NULL,
    root       TEXT NOT NULL,                -- subtree scanned, '' = whole medium
    started    REAL NOT NULL,
    finished   REAL,
    files_seen INTEGER,
    bytes_seen INTEGER,
    hashed     INTEGER                       -- how many actually (re)hashed
);
"""


def db_connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------

def sha256_file(path: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return "sha256:" + h.hexdigest()


# --------------------------------------------------------------------------
# Excludes
# --------------------------------------------------------------------------

DEFAULT_EXCLUDES = [
    ".cache", ".config", ".git", "node_modules", ".Trash-*", ".stfolder",
    ".stversions", "lost+found", "*.tmp", ".DS_Store",
]


def load_excludes(exclude_file: str | None) -> list[str]:
    patterns = list(DEFAULT_EXCLUDES)
    if exclude_file:
        with open(exclude_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def is_excluded(rel_path: str, name: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_path, pat):
            return True
    return False


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_add_medium(conn, args):
    conn.execute(
        "INSERT OR REPLACE INTO media"
        " (medium_id, kind, location_hint, is_backup, notes, last_scanned)"
        " VALUES (?,?,?,?,?,"
        "   (SELECT last_scanned FROM media WHERE medium_id=?))",
        (args.medium_id, args.kind, args.location, int(args.backup),
         args.notes, args.medium_id),
    )
    conn.commit()
    print(f"medium '{args.medium_id}' registered"
          f" (kind={args.kind}, backup={'yes' if args.backup else 'no'})")


def cmd_media(conn, args):
    rows = conn.execute(
        "SELECT m.medium_id, m.kind, m.is_backup, m.location_hint,"
        "       m.last_scanned,"
        "       (SELECT COUNT(*) FROM instances i WHERE i.medium_id=m.medium_id),"
        "       (SELECT COALESCE(SUM(size),0) FROM instances i"
        "         WHERE i.medium_id=m.medium_id)"
        " FROM media m ORDER BY m.medium_id").fetchall()
    if not rows:
        print("no media registered yet — use: holdings add-medium <id> --kind drive")
        return
    print(f"{'MEDIUM':22} {'KIND':12} {'BK':3} {'FILES':>8} {'SIZE':>10}"
          f" {'LAST SCAN':19}  LOCATION")
    for mid, kind, bk, loc, ts, nfiles, nbytes in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"
        print(f"{mid:22} {kind:12} {'y' if bk else '-':3} {nfiles:>8}"
              f" {human_size(nbytes):>10} {when:19}  {loc or ''}")


def cmd_scan(conn, args):
    medium = conn.execute("SELECT medium_id FROM media WHERE medium_id=?",
                          (args.medium_id,)).fetchone()
    if not medium:
        sys.exit(f"unknown medium '{args.medium_id}' — register it first with add-medium")

    mount = Path(args.mount_path).resolve()
    if not mount.is_dir():
        sys.exit(f"mount path {mount} is not a directory")

    patterns = load_excludes(args.exclude_file)
    started = time.time()
    root_rel = args.root.strip("/") if args.root else ""
    scan_root = mount / root_rel if root_rel else mount

    cur = conn.execute(
        "INSERT INTO scans (medium_id, root, started) VALUES (?,?,?)",
        (args.medium_id, root_rel, started))
    scan_id = cur.lastrowid

    files_seen = bytes_seen = hashed = 0
    for dirpath, dirnames, filenames in os.walk(scan_root):
        rel_dir = os.path.relpath(dirpath, mount)
        rel_dir = "" if rel_dir == "." else rel_dir
        # prune excluded directories in-place
        dirnames[:] = [d for d in dirnames
                       if not is_excluded(os.path.join(rel_dir, d), d, patterns)]
        for name in filenames:
            rel_path = os.path.join(rel_dir, name) if rel_dir else name
            if is_excluded(rel_path, name, patterns):
                continue
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue
            if not full.is_file() or full.is_symlink():
                continue
            files_seen += 1
            bytes_seen += st.st_size

            row = conn.execute(
                "SELECT hash, size, mtime FROM instances"
                " WHERE medium_id=? AND path=?",
                (args.medium_id, rel_path)).fetchone()
            if (row and not args.full and row[1] == st.st_size
                    and row[2] is not None and abs(row[2] - st.st_mtime) < 1e-6):
                file_hash = row[0]          # unchanged: reuse known hash
            else:
                try:
                    file_hash = sha256_file(full)
                except OSError as e:
                    print(f"  ! cannot read {rel_path}: {e}", file=sys.stderr)
                    continue
                hashed += 1

            conn.execute(
                "INSERT INTO content (hash, size, first_seen) VALUES (?,?,?)"
                " ON CONFLICT(hash) DO NOTHING",
                (file_hash, st.st_size, time.time()))
            conn.execute(
                "INSERT INTO instances (medium_id, path, hash, size, mtime, seen_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(medium_id, path) DO UPDATE SET"
                "   hash=excluded.hash, size=excluded.size,"
                "   mtime=excluded.mtime, seen_at=excluded.seen_at",
                (args.medium_id, rel_path, file_hash, st.st_size,
                 st.st_mtime, time.time()))
            if files_seen % 500 == 0:
                conn.commit()
                print(f"  … {files_seen} files ({human_size(bytes_seen)})",
                      file=sys.stderr)

    # prune instances under the scanned root that were not seen this scan
    prefix = (root_rel + "/") if root_rel else ""
    pruned = conn.execute(
        "DELETE FROM instances WHERE medium_id=? AND seen_at<?"
        "   AND (path LIKE ? OR ?='')",
        (args.medium_id, started, prefix + "%", prefix)).rowcount

    conn.execute("UPDATE media SET last_scanned=? WHERE medium_id=?",
                 (time.time(), args.medium_id))
    conn.execute(
        "UPDATE scans SET finished=?, files_seen=?, bytes_seen=?, hashed=?"
        " WHERE scan_id=?",
        (time.time(), files_seen, bytes_seen, hashed, scan_id))
    conn.commit()
    print(f"scan of '{args.medium_id}' complete: {files_seen} files"
          f" ({human_size(bytes_seen)}), {hashed} hashed,"
          f" {pruned} vanished entries pruned,"
          f" {time.time()-started:.1f}s")


def cmd_import_restic(conn, args):
    """Ingest `restic ls --json <snapshot>` output so backup copies count.

    Usage:
      restic -r <repo> ls --json latest | holdings import-restic restic-b2 -
    or with a saved file:
      holdings import-restic restic-b2 listing.json
    """
    medium = conn.execute("SELECT medium_id FROM media WHERE medium_id=?",
                          (args.medium_id,)).fetchone()
    if not medium:
        sys.exit(f"unknown medium '{args.medium_id}' — register it first"
                 f" (kind=restic-repo, --backup)")

    stream = sys.stdin if args.listing == "-" else open(args.listing)
    started = time.time()
    count = 0
    with stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "file" or obj.get("struct_type") == "snapshot":
                continue
            path = obj.get("path", "").lstrip("/")
            size = obj.get("size", 0)
            # restic ls --json does not expose content hashes; identity is
            # matched by (size, name) against known content when unique,
            # otherwise recorded as unverified. For exact matching, scan the
            # restored/mounted repo (restic mount) with `holdings scan`.
            h = match_known_content(conn, path, size)
            if h is None:
                h = f"unverified:restic:{args.medium_id}:{path}:{size}"
                conn.execute(
                    "INSERT INTO content (hash, size, first_seen) VALUES (?,?,?)"
                    " ON CONFLICT(hash) DO NOTHING", (h, size, time.time()))
            conn.execute(
                "INSERT INTO instances (medium_id, path, hash, size, mtime, seen_at)"
                " VALUES (?,?,?,?,NULL,?)"
                " ON CONFLICT(medium_id, path) DO UPDATE SET"
                "   hash=excluded.hash, size=excluded.size, seen_at=excluded.seen_at",
                (args.medium_id, path, h, size, time.time()))
            count += 1
    conn.execute(
        "DELETE FROM instances WHERE medium_id=? AND seen_at<?",
        (args.medium_id, started))
    conn.execute("UPDATE media SET last_scanned=? WHERE medium_id=?",
                 (time.time(), args.medium_id))
    conn.commit()
    print(f"imported {count} entries into '{args.medium_id}'"
          f" (exact hashes where filename+size uniquely matched known content;"
          f" run `restic mount` + `holdings scan` for exact verification)")


def match_known_content(conn, path: str, size: int):
    """Match an imported listing entry to known content by basename+size,
    only when the match is unique. Conservative: ambiguous → None."""
    base = os.path.basename(path)
    rows = conn.execute(
        "SELECT DISTINCT i.hash FROM instances i"
        " WHERE i.size=? AND (i.path=? OR i.path=? OR i.path LIKE ?)"
        "   AND i.hash LIKE 'sha256:%' LIMIT 2",
        (size, path, base, "%/" + base)).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return None


def cmd_whereis(conn, args):
    h = resolve_hash(conn, args.target)
    if h is None:
        sys.exit(f"'{args.target}' not found (give a path on a scanned medium,"
                 f" a filename, or a sha256:... hash)")
    rows = conn.execute(
        "SELECT i.medium_id, m.kind, m.is_backup, i.path, i.seen_at,"
        "       m.location_hint"
        " FROM instances i JOIN media m ON m.medium_id=i.medium_id"
        " WHERE i.hash=? ORDER BY m.is_backup DESC, i.medium_id", (h,)).fetchall()
    size = conn.execute("SELECT size FROM content WHERE hash=?", (h,)).fetchone()
    copies = len({r[0] for r in rows})
    backups = len({r[0] for r in rows if r[2]})
    print(f"{h}  ({human_size(size[0]) if size else '?'})")
    print(f"present on {copies} media ({backups} backup):")
    for mid, kind, bk, path, seen, loc in rows:
        when = time.strftime("%Y-%m-%d", time.localtime(seen))
        tag = "backup" if bk else kind
        print(f"  [{tag:11}] {mid:20} {path}   (seen {when}"
              f"{', ' + loc if loc else ''})")


def resolve_hash(conn, target: str):
    if target.startswith(("sha256:", "unverified:")):
        return target
    p = Path(target)
    if p.is_file():
        return sha256_file(p)
    # try exact relative path, then basename match
    row = conn.execute("SELECT hash FROM instances WHERE path=? LIMIT 1",
                       (target.lstrip("/"),)).fetchone()
    if row:
        return row[0]
    rows = conn.execute(
        "SELECT DISTINCT hash FROM instances WHERE path LIKE ? LIMIT 2",
        ("%/" + target,)).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if len(rows) > 1:
        sys.exit(f"'{target}' is ambiguous — several distinct files share that"
                 f" name; give a full path or a hash")
    return None


def cmd_redundancy(conn, args):
    """Content with fewer than --min-copies copies on *backup* media."""
    rows = conn.execute(
        "SELECT * FROM ("
        "  SELECT c.hash, c.size,"
        "    (SELECT COUNT(DISTINCT i.medium_id) FROM instances i"
        "      JOIN media m ON m.medium_id=i.medium_id"
        "      WHERE i.hash=c.hash AND m.is_backup=1) AS bcopies,"
        "    (SELECT COUNT(DISTINCT i2.medium_id) FROM instances i2"
        "      WHERE i2.hash=c.hash) AS copies,"
        "    (SELECT i3.path FROM instances i3 WHERE i3.hash=c.hash LIMIT 1)"
        "      AS example_path"
        "  FROM content c"
        ") WHERE bcopies < ?"
        " ORDER BY bcopies, size DESC LIMIT ?",
        (args.min_copies, args.limit)).fetchall()
    if not rows:
        print(f"OK: everything has at least {args.min_copies}"
              f" backup cop{'y' if args.min_copies==1 else 'ies'}.")
        return
    total = conn.execute(
        "SELECT COUNT(*) FROM content c WHERE"
        "  (SELECT COUNT(DISTINCT i.medium_id) FROM instances i"
        "    JOIN media m ON m.medium_id=i.medium_id"
        "    WHERE i.hash=c.hash AND m.is_backup=1) < ?",
        (args.min_copies,)).fetchone()[0]
    print(f"{total} content objects below {args.min_copies} backup copies"
          f" (showing up to {args.limit}, largest first):")
    print(f"{'BK':>2} {'ALL':>3} {'SIZE':>10}  EXAMPLE PATH")
    for h, size, bcopies, copies, path in rows:
        print(f"{bcopies:>2} {copies:>3} {human_size(size):>10}  {path}")


def cmd_diff(conn, args):
    rows = conn.execute(
        "SELECT i.path, c.size FROM instances i JOIN content c ON c.hash=i.hash"
        " WHERE i.medium_id=? AND i.hash NOT IN"
        "   (SELECT hash FROM instances WHERE medium_id=?)"
        " ORDER BY c.size DESC LIMIT ?",
        (args.medium_a, args.medium_b, args.limit)).fetchall()
    total, tbytes = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(c.size),0) FROM instances i"
        " JOIN content c ON c.hash=i.hash"
        " WHERE i.medium_id=? AND i.hash NOT IN"
        "   (SELECT hash FROM instances WHERE medium_id=?)",
        (args.medium_a, args.medium_b)).fetchone()
    print(f"{total} files ({human_size(tbytes)}) on '{args.medium_a}'"
          f" but not on '{args.medium_b}':")
    for path, size in rows:
        print(f"  {human_size(size):>10}  {path}")


def cmd_only_on(conn, args):
    """Content whose ONLY copies are on the given medium — the danger list."""
    rows = conn.execute(
        "SELECT i.path, c.size FROM instances i JOIN content c ON c.hash=i.hash"
        " WHERE i.medium_id=? AND NOT EXISTS"
        "   (SELECT 1 FROM instances i2 WHERE i2.hash=i.hash"
        "     AND i2.medium_id<>?)"
        " ORDER BY c.size DESC LIMIT ?",
        (args.medium_id, args.medium_id, args.limit)).fetchall()
    total, tbytes = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(c.size),0) FROM instances i"
        " JOIN content c ON c.hash=i.hash"
        " WHERE i.medium_id=? AND NOT EXISTS"
        "   (SELECT 1 FROM instances i2 WHERE i2.hash=i.hash"
        "     AND i2.medium_id<>?)",
        (args.medium_id, args.medium_id)).fetchone()
    print(f"{total} files ({human_size(tbytes)}) exist ONLY on"
          f" '{args.medium_id}':")
    for path, size in rows:
        print(f"  {human_size(size):>10}  {path}")


def cmd_stats(conn, args):
    n_content, t_bytes = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM content").fetchone()
    n_inst = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
    n_media = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    dup = n_inst - n_content if n_content else 0
    print(f"media: {n_media}   unique content: {n_content}"
          f" ({human_size(t_bytes)})   instances: {n_inst}"
          f"   (dedup would fold {dup} duplicate placements)")


def cmd_project_ontodag(conn, args):
    """Emit the `sys:` placement projection for OntoDAG.

    Output: JSON lines, one item per content object:
      {"item": "<hash>", "supercategories": ["sys:on:<medium>", ...,
                                             "sys:type:<ext>", "sys:backup:<n>"]}
    Intended contract (per the agreed design):
      * everything under `sys:` is machine-written, regenerable cache;
      * the ingesting side should DROP all `sys:` memberships and rebuild
        from this stream (idempotent full rebuild, no incremental diffing);
      * human categories are never touched by this stream.
    """
    to_stdout = args.out == "-"
    out = sys.stdout if to_stdout else open(args.out, "w")
    rows = conn.execute(
        "SELECT c.hash,"
        "  (SELECT GROUP_CONCAT(DISTINCT i.medium_id) FROM instances i"
        "    WHERE i.hash=c.hash),"
        "  (SELECT COUNT(DISTINCT i2.medium_id) FROM instances i2"
        "    JOIN media m ON m.medium_id=i2.medium_id"
        "    WHERE i2.hash=c.hash AND m.is_backup=1),"
        "  (SELECT i3.path FROM instances i3 WHERE i3.hash=c.hash LIMIT 1)"
        " FROM content c").fetchall()
    n = 0
    try:
        for h, media_csv, bcopies, path in rows:
            supers = [f"sys:on:{m}" for m in (media_csv or "").split(",") if m]
            ext = os.path.splitext(path or "")[1].lstrip(".").lower()
            if ext:
                supers.append(f"sys:type:{ext}")
            supers.append(f"sys:backup:{bcopies}")
            out.write(json.dumps({"item": h, "supercategories": supers}) + "\n")
            n += 1
    finally:
        out.flush()
        if not to_stdout:          # never close the caller's stdout
            out.close()
    print(f"projected {n} items", file=sys.stderr)


def human_size(n) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="holdings",
        description="Catalog of which media hold which files."
                    " Placement truth in SQLite; semantics belong to OntoDAG.")
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"catalog database (default {DEFAULT_DB};"
                        f" put it in a Syncthing folder to read it everywhere)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add-medium", help="register a medium")
    s.add_argument("medium_id")
    s.add_argument("--kind", required=True,
                   choices=["drive", "laptop", "phone", "restic-repo",
                            "cloud", "other"])
    s.add_argument("--location", help="where it physically lives")
    s.add_argument("--backup", action="store_true",
                   help="counts toward backup redundancy")
    s.add_argument("--notes")
    s.set_defaults(func=cmd_add_medium)

    s = sub.add_parser("media", help="list media")
    s.set_defaults(func=cmd_media)

    s = sub.add_parser("scan", help="scan a mounted medium (or subtree)")
    s.add_argument("medium_id")
    s.add_argument("mount_path")
    s.add_argument("--root", help="only scan this subtree (relative)")
    s.add_argument("--exclude-file", help="extra exclude patterns, one per line")
    s.add_argument("--full", action="store_true",
                   help="rehash everything (ignore mtime+size shortcut)")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("import-restic",
                       help="ingest `restic ls --json` output ('-' = stdin)")
    s.add_argument("medium_id")
    s.add_argument("listing")
    s.set_defaults(func=cmd_import_restic)

    s = sub.add_parser("whereis", help="which media hold this file?")
    s.add_argument("target", help="path, filename, or sha256:... hash")
    s.set_defaults(func=cmd_whereis)

    s = sub.add_parser("redundancy", help="content below N backup copies")
    s.add_argument("--min-copies", type=int, default=2)
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_redundancy)

    s = sub.add_parser("diff", help="on A but not on B")
    s.add_argument("medium_a")
    s.add_argument("medium_b")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("only-on", help="content that exists ONLY on this medium")
    s.add_argument("medium_id")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_only_on)

    s = sub.add_parser("stats", help="catalog totals")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("project-ontodag",
                       help="emit sys: placement projection (JSON lines)")
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_project_ontodag)

    args = p.parse_args(argv)
    conn = db_connect(args.db)
    try:
        args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
