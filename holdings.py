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
    file can be placed in a synced folder, or published read-only, and
    read anywhere). How it travels is not this tool's business: the
    catalog is a path, and everything below works on a local file.
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
    -- Derived from `durability` (and, for a lease, from its expiry) at
    -- refresh time. Kept as a column because every report joins on it.
    is_backup     INTEGER NOT NULL DEFAULT 0,
    durability    TEXT NOT NULL DEFAULT 'working',  -- see DURABILITY
    -- Media at one site fail together: fire, flood, theft, and ransomware
    -- walking every volume that happens to be mounted. Durability says what
    -- kills a copy; site says what kills several at once.
    site          TEXT,
    lease_expires REAL,                      -- when a leased copy lapses
    lease_checked REAL,                      -- when that estimate was taken
    notes         TEXT,
    last_scanned  REAL,
    file_count      INTEGER NOT NULL DEFAULT 0, -- derived from instances
    byte_count      INTEGER NOT NULL DEFAULT 0,
    only_here_count INTEGER NOT NULL DEFAULT 0, -- content held nowhere else
    only_here_bytes INTEGER NOT NULL DEFAULT 0
);

-- How many content objects sit at each backup-copy count. `redundancy`
-- needs a total for a threshold given at runtime, which no single stored
-- number can answer -- but backup_copies takes very few distinct values,
-- so summing this table reads a handful of rows instead of counting
-- tens of thousands.
CREATE TABLE IF NOT EXISTS backup_histogram (
    backup_copies INTEGER PRIMARY KEY,
    content_count INTEGER NOT NULL
);

-- One row (id=1) of totals that `stats` would otherwise scan the whole
-- catalog to produce.
CREATE TABLE IF NOT EXISTS catalog_summary (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    content_count  INTEGER NOT NULL DEFAULT 0,
    content_bytes  INTEGER NOT NULL DEFAULT 0,
    instance_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS content (
    hash       TEXT PRIMARY KEY,             -- 'sha256:...'
    size       INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    -- Derived from instances+media, refreshed after every write. Added by
    -- ALTER TABLE on older catalogs, so they stay last (see instances.name).
    copies        INTEGER NOT NULL DEFAULT 0,-- distinct media holding it
    backup_copies INTEGER NOT NULL DEFAULT 0,-- distinct *backup* media
    backup_sites  INTEGER NOT NULL DEFAULT 0,-- distinct sites among those
    backup_kinds  INTEGER NOT NULL DEFAULT 0,-- distinct kinds among those
    example_path  TEXT                       -- lowest path, for reports
);

CREATE TABLE IF NOT EXISTS instances (
    medium_id TEXT NOT NULL REFERENCES media(medium_id),
    path      TEXT NOT NULL,                 -- path relative to the medium's root
    hash      TEXT NOT NULL REFERENCES content(hash),
    size      INTEGER NOT NULL,
    mtime     REAL,                          -- NULL for imported (e.g. restic) listings
    seen_at   REAL NOT NULL,
    -- Derived from path, stored because it is what gets searched for.
    -- Added by ALTER TABLE on older catalogs, so it must stay last here:
    -- fresh and migrated databases then agree on column order.
    name      TEXT,                          -- basename of path
    only_here INTEGER NOT NULL DEFAULT 0,    -- its content is on no other medium
    PRIMARY KEY (medium_id, path)
);

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

# Created after any migration, since an index can name a column that a
# migration has only just added.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_instances_hash ON instances(hash);
-- The primary key is (medium_id, path), so a lookup by path alone --
-- `whereis <path>`, the commonest query there is -- would otherwise scan
-- every instance. Cheap locally, ruinous over a network: measured at 9+
-- minutes unfinished against a 125 MB catalog published to Swarm, versus
-- 8 pages (0.03% of the file) with the index.
CREATE INDEX IF NOT EXISTS idx_instances_path ON instances(path);
-- And `whereis <bare filename>` searches the basename, which no index can
-- serve from `path` (the pattern starts with a wildcard), hence `name`.
CREATE INDEX IF NOT EXISTS idx_instances_name ON instances(name);
-- `redundancy` reads content in (backup_copies, size) order and stops at
-- --limit, so the index supplies both the filter and the ordering.
CREATE INDEX IF NOT EXISTS idx_content_redundancy
    ON content(backup_copies, size DESC);
-- `only-on` is the danger list, so it has to be usable. Driving it from
-- the singletons (copies=1) in size order and probing instances by hash
-- costs the size of that set; driving it from the medium's rows costs the
-- whole medium, which measured at minutes over a network.
CREATE INDEX IF NOT EXISTS idx_content_singletons
    ON content(copies, size DESC);
-- `only-on` wants one medium's singletons, largest first. Carrying the
-- flag on the instance row lets one index range scan answer that, instead
-- of walking every singleton in the catalog and probing for its medium.
CREATE INDEX IF NOT EXISTS idx_instances_only_here
    ON instances(medium_id, only_here, size DESC);
"""


def basename(path: str) -> str:
    """The name part of a stored (always '/'-separated) relative path."""
    return path.rpartition("/")[2]


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an older catalog up to the current schema, in place.

    Deliberately not "drop it and rescan". The contract says everything is
    regenerable by re-scanning, but that is not a licence to discard the
    catalog: its whole value is facts about media that are *not* reachable
    right now — a drive in a safe in another country cannot be rescanned on
    demand, and dropping the file would lose exactly the part that cannot be
    rebuilt. Migrations here are additive and idempotent.
    """
    added = False
    for table, column, decl in [
        ("instances", "name", "TEXT"),
        ("instances", "only_here", "INTEGER NOT NULL DEFAULT 0"),
        ("media", "durability", "TEXT NOT NULL DEFAULT 'working'"),
        ("media", "site", "TEXT"),
        ("content", "backup_sites", "INTEGER NOT NULL DEFAULT 0"),
        ("content", "backup_kinds", "INTEGER NOT NULL DEFAULT 0"),
        ("media", "lease_expires", "REAL"),
        ("media", "lease_checked", "REAL"),
        ("media", "only_here_count", "INTEGER NOT NULL DEFAULT 0"),
        ("media", "only_here_bytes", "INTEGER NOT NULL DEFAULT 0"),
        ("content", "copies", "INTEGER NOT NULL DEFAULT 0"),
        ("content", "backup_copies", "INTEGER NOT NULL DEFAULT 0"),
        ("content", "example_path", "TEXT"),
        ("media", "file_count", "INTEGER NOT NULL DEFAULT 0"),
        ("media", "byte_count", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added = True

    stale = conn.execute(
        "SELECT medium_id, path FROM instances WHERE name IS NULL").fetchall()
    if stale:
        conn.executemany(
            "UPDATE instances SET name=? WHERE medium_id=? AND path=?",
            [(basename(path), mid, path) for mid, path in stale])
    if added:
        # An existing catalog asserted is_backup by hand. The honest
        # translation is "independent": it was claimed to survive deletion
        # of the original, and nothing recorded says otherwise. Anyone who
        # meant a sync mirror now has to say so -- which is the point.
        conn.execute("UPDATE media SET durability ="
                     "  CASE WHEN is_backup THEN 'independent'"
                     "       ELSE 'working' END"
                     " WHERE durability = 'working' AND is_backup = 1")
        # The counts default to 0, which would read as "no backups anywhere"
        # until something refreshed them -- so refresh before anyone can ask.
        refresh_derived(conn)
    if stale or added:
        conn.commit()


def db_connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.executescript(INDEXES)
    return conn


# --------------------------------------------------------------------------
# Published catalogs (optional)
# --------------------------------------------------------------------------

# A catalog is normally a local file. It can also be *published* read-only and
# opened by URL, which is what lets a phone or a second laptop answer `whereis`
# without holding the file or pairing with anything. This is an optional extra
# (`pip install 'holdings[swarm]'`): nothing here is imported, installed or
# required for a local catalog, which remains the default and complete path.
#
# `bzz://` pins an immutable version, `bzzf://` follows a feed to the latest.
# `file://` and `memory://` are swarmlite's own local forms — the identical
# code path with no node involved, which is how to try the read path (or read
# a local catalog strictly read-only) before publishing anything.
REMOTE_SCHEMES = ("bzz://", "bzzf://", "file://", "memory://")

_WRITE_ON_PUBLISHED = """\
A published catalog is read-only. Placement is written where the scanning
happens, and published afterwards:
    holdings --db <local.sqlite> {cmd} ...
    swarmlite publish <local.sqlite> --encrypt --feed holdings"""


def is_remote_url(db: str) -> bool:
    return db.startswith(REMOTE_SCHEMES)


def warn_if_uncheckpointed(url: str) -> None:
    """A `file://` read ignores an un-checkpointed WAL, silently.

    swarmlite's read-only VFS reports the WAL absent. That is right for a
    published artifact — `swarmlite publish` checkpoints into
    journal_mode=DELETE first, so `bzz://` and `bzzf://` are always whole —
    but a `file://` URL can point at a live catalog with a writer's sidecar
    beside it. Measured: an insert sitting in the WAL is simply not there.
    Staleness in a placement catalog reads as "no backup exists", which is
    the one wrong answer this tool must not give quietly.
    """
    if not url.startswith("file://"):
        return
    wal = Path(url[len("file://"):] + "-wal")
    try:
        empty = wal.stat().st_size == 0
    except OSError:
        return
    if not empty:
        print(f"warning: '{wal.name}' is present, so this reads the last"
              f" checkpointed state — writes still in the WAL are invisible."
              f" Close the writer first (or publish, which checkpoints).",
              file=sys.stderr)


def remote_connect(url: str):
    """Open a published catalog read-only, via swarmlite.

    swarmlite maps SQLite's 4 KB pages onto ranged reads, so an indexed
    lookup such as `whereis` fetches a handful of pages rather than the
    whole catalog. The import is deliberately lazy and deliberately late:
    a local catalog never pays for it, and a machine without the extra
    only ever meets this error by explicitly asking for a URL.
    """
    try:
        import swarmlite
    except ModuleNotFoundError:
        sys.exit(f"opening '{url}' needs the optional published-catalog"
                 f" reader:\n"
                 f"    pip install 'holdings[swarm]'\n"
                 f"A local catalog file needs nothing beyond the stdlib.")
    warn_if_uncheckpointed(url)
    return swarmlite.connect(url)


# A synced folder refreshes itself; a published catalog does not. `bzz://`
# names one immutable version forever, and a `bzzf://` feed only moves when
# someone republishes -- so a reader can be looking at placement from months
# ago with nothing on screen to say so.
DEFAULT_MAX_SCAN_AGE_DAYS = 30.0


def warn_if_stale(conn, max_age_days: float) -> None:
    """Warn when nothing in a published catalog has been scanned recently.

    Deliberately measures scan age, not publication age. The tempting check
    -- compare the feed's last update against the catalog's newest scan --
    cannot say anything: a published catalog was written before it was
    published, so that gap is always small and always reassuring, including
    when the writer stopped scanning a year ago. What a reader can actually
    act on is how old the underlying facts are.
    """
    if max_age_days <= 0:
        return
    newest = conn.execute(QUERIES["newest_scan"]).fetchone()[0]
    if newest is None:
        return
    age = (time.time() - newest) / 86400
    if age < max_age_days:
        return
    print(f"warning: nothing in this catalog has been scanned since "
          f"{time.strftime('%Y-%m-%d', time.localtime(newest))}"
          f" ({age:.0f} days ago) -- a published catalog is a snapshot and"
          f" does not refresh itself. Republish after scanning."
          f" (--max-scan-age 0 silences this.)", file=sys.stderr)


def open_catalog(db: str, *, writes: bool, cmd: str,
                 max_scan_age: float = DEFAULT_MAX_SCAN_AGE_DAYS):
    """Connect to a catalog, local or published, per the `--db` path given."""
    if not is_remote_url(db):
        return db_connect(db)
    if writes:
        sys.exit(f"'{cmd}' writes to the catalog, but '{db}' is published.\n"
                 + _WRITE_ON_PUBLISHED.format(cmd=cmd))
    conn = remote_connect(db)
    warn_if_stale(conn, max_scan_age)
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

# --------------------------------------------------------------------------
# What a copy survives
# --------------------------------------------------------------------------

# `is_backup` used to be asserted by hand, and it answered "is this a backup?"
# for media that fail in quite different ways. A second drive and a Dropbox
# folder are both "two places the file is"; only one of them still has the
# file after you delete the original, because the other propagates the
# deletion. Since `redundancy --exit-code` turns this into a gate that
# scripts trust, the distinction has to live in the data.
#
# The test each class answers: *if the original is deleted, does this copy
# survive, and for how long?*
DURABILITY = {
    "working":     "the live copy you edit -- not a backup of anything",
    "independent": "survives deletion of the original; fails only by its own"
                   " loss (a second drive, a restic repo)",
    "mirror":      "a sync target: your deletion reaches it (Syncthing,"
                   " Dropbox, Drive). Protects against losing a device and"
                   " nothing else",
    "leased":      "survives deletion, but lapses on a schedule unless"
                   " renewed (Swarm postage, prepaid storage)",
    "hosted":      "survives deletion, but someone else decides how long"
                   " (GitHub, Hugging Face) or whether to keep seeding it"
                   " (Radicle)",
}

# Classes whose copies are still there after an `rm` that propagates
# everywhere it can. A lease is included: it expires, which `redundancy`
# reports as a separate, time-dependent condition rather than by pretending
# the copy does not exist.
COUNTS_AS_BACKUP = ("independent", "leased", "hosted")

# How long a lease must have left before it is treated as a dependable
# backup copy. A node's TTL is an estimate at the current storage price; if
# the price rises the batch drains faster than quoted, so the number is an
# optimistic bound and wants headroom. Matches the runbook's
# `swarmlite stamps --check --min-ttl`.
DEFAULT_LEASE_MARGIN_DAYS = 14.0


def parse_when(text: str) -> float:
    """A lease expiry: an ISO date, or a duration from now ('30d', '4w')."""
    text = text.strip()
    units = {"h": 3600, "d": 86400, "w": 604800}
    if text and text[-1] in units and text[:-1].replace(".", "", 1).isdigit():
        return time.time() + float(text[:-1]) * units[text[-1]]
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%d"))
    except ValueError:
        raise SystemExit(f"cannot read '{text}' as a date (YYYY-MM-DD) or a"
                         f" duration from now (e.g. 30d, 4w)")


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
# The read queries
# --------------------------------------------------------------------------

# Named once because there is now more than one reader. The browser viewer
# (web/) runs these same strings against the same published file, and every
# report query here has already been rewritten once -- when the aggregates
# moved to write time -- so a second copy of the SQL would have silently
# drifted that day. `web/queries.json` is generated from this dict and a
# test fails if the committed copy disagrees.
#
# Read-only by construction: nothing here writes, which is what makes them
# safe to hand to a reader that has no write path at all.
QUERIES = {
    "media":
        "SELECT medium_id, kind, is_backup, location_hint, last_scanned,"
        "       file_count, byte_count, only_here_count, only_here_bytes,"
        "       durability, lease_expires, site"
        " FROM media ORDER BY medium_id",
    "summary":
        "SELECT content_count, content_bytes, instance_count"
        " FROM catalog_summary WHERE id=1",
    "media_count":
        "SELECT COUNT(*) AS n FROM media",
    "resolve_by_path":
        "SELECT hash FROM instances WHERE path=? LIMIT 1",
    "resolve_by_name":
        "SELECT DISTINCT hash FROM instances WHERE name=? LIMIT 2",
    "content_size":
        "SELECT size FROM content WHERE hash=?",
    "placements":
        "SELECT i.medium_id, m.kind, m.is_backup, i.path, i.seen_at,"
        "       m.location_hint"
        " FROM instances i JOIN media m ON m.medium_id=i.medium_id"
        " WHERE i.hash=? ORDER BY m.is_backup DESC, i.medium_id",
    "redundancy_rows":
        "SELECT hash, size, backup_copies, copies, example_path FROM content"
        " WHERE backup_copies < ?"
        " ORDER BY backup_copies, size DESC LIMIT ?",
    "redundancy_total":
        "SELECT COALESCE(SUM(content_count), 0) AS n FROM backup_histogram"
        " WHERE backup_copies < ?",
    # The full 3-2-1 form. Not index-served -- the OR defeats
    # idx_content_redundancy -- so it is opt-in, and the default path above
    # keeps its index and its page economy.
    "policy_rows":
        "SELECT hash, size, backup_copies, copies, example_path,"
        "       backup_sites, backup_kinds FROM content"
        " WHERE backup_copies < ? OR backup_sites < ? OR backup_kinds < ?"
        " ORDER BY backup_copies, size DESC LIMIT ?",
    "policy_total":
        "SELECT COUNT(*) AS n FROM content"
        " WHERE backup_copies < ? OR backup_sites < ? OR backup_kinds < ?",
    "only_on_rows":
        "SELECT path, size FROM instances"
        " WHERE medium_id=? AND only_here=1"
        " ORDER BY size DESC LIMIT ?",
    "only_on_totals":
        "SELECT only_here_count, only_here_bytes FROM media WHERE medium_id=?",
    "newest_scan":
        "SELECT MAX(last_scanned) AS newest FROM media",
}


# --------------------------------------------------------------------------
# Derived state
# --------------------------------------------------------------------------

# Some columns are a cache over the base tables -- `instances.name` is the
# basename of `instances.path`. They exist so the queries people actually run
# are index lookups rather than scans.
#
# A cache that is quietly wrong is worse here than no cache at all. These
# columns feed the question "do I have a backup of this?", and a stale yes is
# the single answer this tool must never give: it is the one that ends with
# someone wiping a drive. So the recomputation stays available, the writers
# refresh it, and a test asserts the two agree.

# One grouped pass over instances: what every content row's derived columns
# should be. MIN(path) rather than an arbitrary one, so the value is
# reproducible and drift is therefore detectable.
_GROUPED = """
SELECT i.hash AS hash,
       COUNT(DISTINCT i.medium_id) AS copies,
       COUNT(DISTINCT CASE WHEN m.is_backup THEN i.medium_id END)
           AS backup_copies,
       -- A medium with no site recorded cannot be claimed to be separate
       -- from another, so they all collapse into one unknown site. Being
       -- conservative here means under-counting separation, never over.
       COUNT(DISTINCT CASE WHEN m.is_backup
                           THEN COALESCE(m.site, '?') END) AS backup_sites,
       COUNT(DISTINCT CASE WHEN m.is_backup THEN m.kind END) AS backup_kinds,
       MIN(i.path) AS example_path
  FROM instances i JOIN media m ON m.medium_id = i.medium_id
 GROUP BY i.hash
"""

_RECOMPUTE = f"""
SELECT c.hash, COALESCE(x.copies, 0), COALESCE(x.backup_copies, 0),
       COALESCE(x.backup_sites, 0), COALESCE(x.backup_kinds, 0),
       x.example_path
  FROM content c LEFT JOIN ({_GROUPED}) x ON x.hash = c.hash
"""


_BACKUP_CLASSES_SQL = ", ".join(f"'{c}'" for c in COUNTS_AS_BACKUP)


def lease_risks(conn, margin_days: float) -> list[tuple[str, str]]:
    """Leased media that cannot be relied on as a backup copy right now.

    Time-dependent, so it is computed at read time rather than materialised:
    a lease lapses with no write happening anywhere, and a stored answer
    would go quietly wrong between scans.
    """
    now = time.time()
    risks = []
    for mid, expires, checked in conn.execute(
            "SELECT medium_id, lease_expires, lease_checked FROM media"
            " WHERE durability='leased' ORDER BY medium_id"):
        if expires is None:
            risks.append((mid, "no expiry recorded"))
            continue
        left = (expires - now) / 86400
        if left <= 0:
            risks.append((mid, f"lapsed {-left:.0f} days ago"))
        elif left < margin_days:
            risks.append((mid, f"{left:.0f} days left"))
        elif checked is not None and (now - checked) / 86400 > margin_days:
            # The quote was an estimate at the price of the day; an old one
            # has had time to be overtaken.
            risks.append((mid, f"{left:.0f} days left, but that estimate is"
                               f" {(now - checked) / 86400:.0f} days old"))
    return risks


def refresh_derived(conn) -> None:
    """Recompute every derived column from the base tables.

    Wholesale, not incrementally -- the same choice the projection contract
    makes, and for the same reason: staleness is a permitted failure mode,
    drift is not, and a full rebuild cannot half-apply. It runs after each
    write command, which is rare (a scan already walked a filesystem), so
    the cost lands where nobody is waiting on a query.

    It must run after `add-medium` too, not just after a scan: flipping
    --backup on one medium changes backup_copies for everything on it.
    """
    # First, because the content counts below are computed from it: what
    # counts as a backup follows from the medium's class, and is no longer
    # something anyone asserts directly.
    conn.execute(
        "UPDATE media SET is_backup ="
        f"  CASE WHEN durability IN ({_BACKUP_CLASSES_SQL}) THEN 1 ELSE 0 END")
    # Staged through a temp table rather than UPDATE..FROM, which needs
    # SQLite 3.33 -- newer than `requires-python = ">=3.10"` guarantees.
    conn.execute("DROP TABLE IF EXISTS temp._counts")
    conn.execute(f"CREATE TEMP TABLE _counts AS {_GROUPED}")
    conn.execute("CREATE INDEX temp._counts_hash ON _counts(hash)")
    conn.execute(
        "UPDATE content SET"
        "  copies = COALESCE("
        "    (SELECT copies FROM _counts WHERE hash = content.hash), 0),"
        "  backup_copies = COALESCE("
        "    (SELECT backup_copies FROM _counts WHERE hash = content.hash), 0),"
        "  backup_sites = COALESCE("
        "    (SELECT backup_sites FROM _counts WHERE hash = content.hash), 0),"
        "  backup_kinds = COALESCE("
        "    (SELECT backup_kinds FROM _counts WHERE hash = content.hash), 0),"
        "  example_path ="
        "    (SELECT example_path FROM _counts WHERE hash = content.hash)")
    conn.execute("DROP TABLE temp._counts")
    conn.execute(
        "UPDATE instances SET only_here = COALESCE("
        "  (SELECT c.copies = 1 FROM content c WHERE c.hash = instances.hash),"
        "  0)")
    # After content.copies, which only_here_* depends on.
    conn.execute(
        "UPDATE media SET"
        "  file_count = (SELECT COUNT(*) FROM instances"
        "                 WHERE medium_id = media.medium_id),"
        "  byte_count = COALESCE((SELECT SUM(size) FROM instances"
        "                          WHERE medium_id = media.medium_id), 0),"
        "  only_here_count = (SELECT COUNT(*) FROM instances i"
        "     JOIN content c ON c.hash = i.hash"
        "    WHERE i.medium_id = media.medium_id AND c.copies = 1),"
        "  only_here_bytes = COALESCE((SELECT SUM(c.size) FROM instances i"
        "     JOIN content c ON c.hash = i.hash"
        "    WHERE i.medium_id = media.medium_id AND c.copies = 1), 0)")
    conn.execute("DELETE FROM backup_histogram")
    conn.execute(
        "INSERT INTO backup_histogram (backup_copies, content_count)"
        " SELECT backup_copies, COUNT(*) FROM content GROUP BY backup_copies")
    # Query plans here are genuinely data-dependent -- whether `only-on` is
    # better driven from the singleton set or from the medium depends on
    # their relative sizes -- so give the planner current statistics rather
    # than forcing a join order that is right for one shape of catalog.
    conn.execute("ANALYZE")
    conn.execute(
        "INSERT INTO catalog_summary (id, content_count, content_bytes,"
        "                             instance_count)"
        " VALUES (1, (SELECT COUNT(*) FROM content),"
        "            (SELECT COALESCE(SUM(size), 0) FROM content),"
        "            (SELECT COUNT(*) FROM instances))"
        " ON CONFLICT(id) DO UPDATE SET"
        "   content_count=excluded.content_count,"
        "   content_bytes=excluded.content_bytes,"
        "   instance_count=excluded.instance_count")


def derived_drift(conn) -> list[str]:
    """Describe every disagreement between stored derived state and a fresh
    recomputation from the base tables. Empty list means consistent."""
    problems = []
    wrong = [path for path, name in
             conn.execute("SELECT path, name FROM instances")
             if name != basename(path)]
    if wrong:
        problems.append(
            f"instances.name disagrees with path on {len(wrong)} row(s), "
            f"e.g. {wrong[0]!r}")

    claimed = conn.execute(
        "SELECT COUNT(*) FROM media WHERE is_backup <>"
        f"  (CASE WHEN durability IN ({_BACKUP_CLASSES_SQL}) THEN 1 ELSE 0 END)"
    ).fetchone()[0]
    if claimed:
        problems.append(
            f"media.is_backup disagrees with durability on {claimed} row(s)")

    unknown = [m for m, in conn.execute(
        "SELECT medium_id FROM media WHERE durability NOT IN ("
        + ", ".join("?" * len(DURABILITY)) + ")", tuple(DURABILITY))]
    if unknown:
        problems.append(f"media with an unknown durability class: {unknown}")

    leaseless = [m for m, in conn.execute(
        "SELECT medium_id FROM media"
        " WHERE durability='leased' AND lease_expires IS NULL")]
    if leaseless:
        problems.append(
            f"leased media with no recorded expiry: {leaseless}"
            " -- they count as backup copies with no end date")

    flags = conn.execute(
        "SELECT COUNT(*) FROM instances i LEFT JOIN content c"
        "    ON c.hash = i.hash"
        " WHERE i.only_here <> COALESCE(c.copies = 1, 0)").fetchone()[0]
    if flags:
        problems.append(
            f"instances.only_here disagrees with content.copies"
            f" on {flags} row(s)")

    expected = {h: row for h, *row in conn.execute(_RECOMPUTE)}
    stored = {h: row for h, *row in conn.execute(
        "SELECT hash, copies, backup_copies, backup_sites, backup_kinds,"
        "       example_path FROM content")}
    off = [h for h in stored if stored[h] != expected.get(h)]
    if off:
        problems.append(
            f"content copy counts are stale on {len(off)} row(s), "
            f"e.g. {off[0]}: stored {stored[off[0]]}, "
            f"recomputed {expected.get(off[0])}")

    for mid, *stored_totals in conn.execute(
            "SELECT medium_id, file_count, byte_count, only_here_count,"
            "       only_here_bytes FROM media"):
        real = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(i.size), 0),"
            "       COALESCE(SUM(c.copies = 1), 0),"
            "       COALESCE(SUM(CASE WHEN c.copies = 1 THEN c.size END), 0)"
            "  FROM instances i JOIN content c ON c.hash = i.hash"
            " WHERE i.medium_id=?", (mid,)).fetchone()
        if tuple(stored_totals) != tuple(real):
            problems.append(f"media '{mid}' totals stale: stored "
                            f"{tuple(stored_totals)}, recomputed {tuple(real)}")

    hist = dict(conn.execute(
        "SELECT backup_copies, content_count FROM backup_histogram"))
    real_hist = dict(conn.execute(
        "SELECT backup_copies, COUNT(*) FROM content GROUP BY backup_copies"))
    if hist != real_hist:
        problems.append(f"backup histogram stale: stored {hist},"
                        f" recomputed {real_hist}")

    row = conn.execute("SELECT content_count, content_bytes, instance_count"
                       " FROM catalog_summary WHERE id=1").fetchone()
    real = conn.execute(
        "SELECT (SELECT COUNT(*) FROM content),"
        "       (SELECT COALESCE(SUM(size), 0) FROM content),"
        "       (SELECT COUNT(*) FROM instances)").fetchone()
    if row is None or tuple(row) != tuple(real):
        problems.append(f"catalog summary stale: stored {row},"
                        f" recomputed {tuple(real)}")
    return problems


def cmd_check(conn, args):
    problems = derived_drift(conn)
    # Not drift -- the catalog is internally consistent -- but the same
    # question a reader is really asking: can I rely on what this says?
    problems += [f"leased medium '{m}': {w}"
                 for m, w in lease_risks(conn, DEFAULT_LEASE_MARGIN_DAYS)]
    if emit_json(args, {"consistent": not problems, "problems": problems}):
        if problems:
            raise SystemExit(1)
        return
    if not problems:
        print("derived state is consistent with the base tables")
        return
    for p in problems:
        print(f"DRIFT: {p}", file=sys.stderr)
    sys.exit("re-run `scan` on the affected media to rebuild derived state"
             " (it is a cache; the base tables are unaffected)")


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def emit_json(args, payload) -> bool:
    """Print `payload` as one JSON object if --json was asked for.

    The alternative considered was an HTTP API, which would have put a
    port, a process and a dependency in front of data that is already a
    plain SQLite file anyone can open. A flag composes with pipes, works
    over ssh, and needs nothing running.
    """
    if not getattr(args, "json", False):
        return False
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return True


def policy_exit(args, violations: int) -> None:
    """Exit non-zero when --exit-code was given and the policy is violated.

    The README has called `redundancy` a checkable report since v0.1, but
    it exited 0 whatever it found, so nothing could actually check it.
    Same shape as `swarmlite stamps --check --min-ttl`, which the
    publishing runbook already puts in a cron line.
    """
    if getattr(args, "exit_code", False) and violations:
        raise SystemExit(1)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_add_medium(conn, args):
    durability = args.durability or ("independent" if args.backup
                                     else "working")
    known = conn.execute("SELECT lease_expires FROM media WHERE medium_id=?",
                         (args.medium_id,)).fetchone()
    if (durability == "leased" and not args.lease_expires
            and not (known and known[0])):
        sys.exit("--durability leased needs --lease-expires: a lease with no"
                 " end date would count as a backup copy forever, which is"
                 " the one thing a lease is not")
    if args.lease_expires and durability != "leased":
        sys.exit(f"--lease-expires applies to --durability leased,"
                 f" not '{durability}'")

    expires = parse_when(args.lease_expires) if args.lease_expires else None
    # INSERT OR REPLACE rewrites the whole row, so anything not restated
    # here has to be carried over: the scan time, and a lease recorded by an
    # earlier call that this one is not changing.
    conn.execute(
        "INSERT OR REPLACE INTO media"
        " (medium_id, kind, location_hint, durability, site, notes,"
        "  last_scanned, lease_expires, lease_checked)"
        " VALUES (?,?,?,?,"
        "   COALESCE(?, (SELECT site FROM media WHERE medium_id=?)),?,"
        "   (SELECT last_scanned FROM media WHERE medium_id=?),"
        "   COALESCE(?, (SELECT lease_expires FROM media WHERE medium_id=?)),"
        "   COALESCE(?, (SELECT lease_checked FROM media WHERE medium_id=?)))",
        (args.medium_id, args.kind, args.location, durability,
         args.site, args.medium_id, args.notes,
         args.medium_id,
         expires, args.medium_id,
         time.time() if expires else None, args.medium_id),
    )
    refresh_derived(conn)       # the class here changes every backup_copies
    conn.commit()
    note = DURABILITY[durability].split(" (")[0].split(";")[0].split(" -- ")[0]
    print(f"medium '{args.medium_id}' registered"
          f" (kind={args.kind}, durability={durability}: {note})")


def cmd_media(conn, args):
    rows = conn.execute(
        QUERIES["media"]).fetchall()
    fields = ("medium_id", "kind", "is_backup", "location_hint",
              "last_scanned", "file_count", "byte_count",
              "only_here_count", "only_here_bytes",
              "durability", "lease_expires", "site")
    if emit_json(args, {"media": [dict(zip(fields, r)) for r in rows]}):
        return
    if not rows:
        print("no media registered yet — use: holdings add-medium <id> --kind drive")
        return
    print(f"{'MEDIUM':22} {'KIND':12} {'SURVIVES':12} {'SITE':10}"
          f" {'FILES':>8} {'SIZE':>10} {'LAST SCAN':19}  LOCATION")
    for (mid, kind, _bk, loc, ts, nfiles, nbytes, _only_n, _only_b,
         durability, expires, site) in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"
        shown = durability
        if durability == "leased" and expires:
            shown = f"{durability}*" if expires <= time.time() else durability
        print(f"{mid:22} {kind:12} {shown:12} {(site or '-'):10}"
              f" {nfiles:>8} {human_size(nbytes):>10} {when:19}"
              f"  {loc or ''}")


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
    # Paths this scan could not read. Kept apart from "not seen", because a
    # drive that is failing and a drive you tidied up look identical to the
    # prune below, and only one of them means the copy is gone.
    unreadable: list[str] = []
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
            except OSError as e:
                unreadable.append(rel_path)
                print(f"  ! cannot stat {rel_path}: {e}", file=sys.stderr)
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
                    unreadable.append(rel_path)
                    print(f"  ! cannot read {rel_path}: {e}", file=sys.stderr)
                    continue
                hashed += 1

            conn.execute(
                "INSERT INTO content (hash, size, first_seen) VALUES (?,?,?)"
                " ON CONFLICT(hash) DO NOTHING",
                (file_hash, st.st_size, time.time()))
            conn.execute(
                "INSERT INTO instances"
                " (medium_id, path, name, hash, size, mtime, seen_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(medium_id, path) DO UPDATE SET"
                "   name=excluded.name, hash=excluded.hash,"
                "   size=excluded.size,"
                "   mtime=excluded.mtime, seen_at=excluded.seen_at",
                (args.medium_id, rel_path, basename(rel_path), file_hash,
                 st.st_size, st.st_mtime, time.time()))
            if files_seen % 500 == 0:
                conn.commit()
                print(f"  … {files_seen} files ({human_size(bytes_seen)})",
                      file=sys.stderr)

    # Spare what we merely failed to read. Without this, an I/O error means
    # the row is not re-stamped, the prune below deletes it, and a failing
    # drive is recorded as one whose files were deliberately removed --
    # quietly lowering the copy count on content that is still there.
    if unreadable:
        conn.executemany(
            "UPDATE instances SET seen_at=? WHERE medium_id=? AND path=?",
            [(time.time(), args.medium_id, pth) for pth in unreadable])

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
    refresh_derived(conn)
    conn.commit()
    print(f"scan of '{args.medium_id}' complete: {files_seen} files"
          f" ({human_size(bytes_seen)}), {hashed} hashed,"
          f" {pruned} vanished entries pruned,"
          f" {time.time()-started:.1f}s")
    if unreadable:
        print(f"WARNING: {len(unreadable)} file(s) on '{args.medium_id}' could"
              f" not be read. Their catalog entries were kept, not pruned --"
              f" but this medium could not confirm them, and unreadable files"
              f" on a backup medium are how a drive announces it is failing.",
              file=sys.stderr)


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
                "INSERT INTO instances"
                " (medium_id, path, name, hash, size, mtime, seen_at)"
                " VALUES (?,?,?,?,?,NULL,?)"
                " ON CONFLICT(medium_id, path) DO UPDATE SET"
                "   name=excluded.name, hash=excluded.hash,"
                "   size=excluded.size, seen_at=excluded.seen_at",
                (args.medium_id, path, basename(path), h, size, time.time()))
            count += 1
    conn.execute(
        "DELETE FROM instances WHERE medium_id=? AND seen_at<?",
        (args.medium_id, started))
    conn.execute("UPDATE media SET last_scanned=? WHERE medium_id=?",
                 (time.time(), args.medium_id))
    refresh_derived(conn)
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
    rows = conn.execute(QUERIES["placements"], (h,)).fetchall()
    size = conn.execute(QUERIES["content_size"], (h,)).fetchone()
    copies = len({r[0] for r in rows})
    backups = len({r[0] for r in rows if r[2]})
    if emit_json(args, {
            "hash": h,
            "size": size[0] if size else None,
            "copies": copies,
            "backup_copies": backups,
            "placements": [
                dict(zip(("medium_id", "kind", "is_backup", "path",
                          "seen_at", "location_hint"), r)) for r in rows]}):
        return
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
    row = conn.execute(QUERIES["resolve_by_path"],
                       (target.lstrip("/"),)).fetchone()
    if row:
        return row[0]
    rows = conn.execute(QUERIES["resolve_by_name"], (target,)).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if len(rows) > 1:
        sys.exit(f"'{target}' is ambiguous — several distinct files share that"
                 f" name; give a full path or a hash")
    return None


def cmd_redundancy(conn, args):
    """Content with fewer than --min-copies copies on *backup* media."""
    # The default question -- "fewer than N backup copies" -- stays on the
    # index. Asking the full 3-2-1 question costs a scan, so it happens only
    # when the extra thresholds are actually set.
    full = args.min_sites > 1 or args.min_kinds > 1
    if full:
        rows = conn.execute(
            QUERIES["policy_rows"],
            (args.min_copies, args.min_sites, args.min_kinds,
             args.limit)).fetchall()
        total = conn.execute(
            QUERIES["policy_total"],
            (args.min_copies, args.min_sites, args.min_kinds)).fetchone()[0]
    else:
        rows = conn.execute(QUERIES["redundancy_rows"],
                            (args.min_copies, args.limit)).fetchall()
        total = conn.execute(QUERIES["redundancy_total"],
                             (args.min_copies,)).fetchone()[0]
    # A lease can lapse with no write happening, so the materialised counts
    # above can be right as of the last scan and wrong now. Checked here,
    # and counted as a violation: a backup you have stopped paying for is
    # not a backup, and this is the report that gates other people's cron.
    risks = lease_risks(conn, args.lease_margin)

    cols = ("hash", "size", "backup_copies", "copies", "example_path",
            "backup_sites", "backup_kinds")
    if emit_json(args, {
            "min_copies": args.min_copies,
            "min_sites": args.min_sites,
            "min_kinds": args.min_kinds,
            "below": total,
            "leases_at_risk": [{"medium_id": m, "status": w}
                               for m, w in risks],
            "items": [dict(zip(cols, r)) for r in rows]}):
        policy_exit(args, total + len(risks))
        return
    for mid, why in risks:
        print(f"AT RISK: leased medium '{mid}' -- {why}", file=sys.stderr)
    def plural(n, word):
        return f"{n} {word}{'' if n == 1 else 's'}"

    policy = plural(args.min_copies, "backup copy").replace("copys", "copies")
    if full:
        policy += (f", {plural(args.min_sites, 'site')}"
                   f", {plural(args.min_kinds, 'kind')}")
    if not rows:
        print(f"OK: everything meets {policy}.")
        policy_exit(args, total + len(risks))
        return
    print(f"{total} content objects below {policy}"
          f" (showing up to {args.limit}, largest first):")
    head = f"{'BK':>2} {'ALL':>3}" + (f" {'SITE':>4} {'KIND':>4}" if full else "")
    print(f"{head} {'SIZE':>10}  EXAMPLE PATH")
    for row in rows:
        h, size, bcopies, copies, path = row[:5]
        extra = f" {row[5]:>4} {row[6]:>4}" if full else ""
        print(f"{bcopies:>2} {copies:>3}{extra}"
              f" {human_size(size):>10}  {path}")
    policy_exit(args, total + len(risks))


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
    if emit_json(args, {
            "a": args.medium_a, "b": args.medium_b,
            "files": total, "bytes": tbytes,
            "items": [{"path": pth, "size": sz} for pth, sz in rows]}):
        return
    print(f"{total} files ({human_size(tbytes)}) on '{args.medium_a}'"
          f" but not on '{args.medium_b}':")
    for path, size in rows:
        print(f"  {human_size(size):>10}  {path}")


def cmd_only_on(conn, args):
    """Content whose ONLY copies are on the given medium — the danger list."""
    # copies=1 means exactly one medium holds it; combined with an instance
    # on this medium, that medium is this one. Driven from content so the
    # cost is the size of the singleton set, not the size of the medium.
    rows = conn.execute(QUERIES["only_on_rows"],
                        (args.medium_id, args.limit)).fetchall()
    total, tbytes = conn.execute(
        QUERIES["only_on_totals"], (args.medium_id,)).fetchone() or (0, 0)
    if emit_json(args, {
            "medium_id": args.medium_id, "files": total, "bytes": tbytes,
            "items": [{"path": pth, "size": sz} for pth, sz in rows]}):
        policy_exit(args, total)
        return
    print(f"{total} files ({human_size(tbytes)}) exist ONLY on"
          f" '{args.medium_id}':")
    for path, size in rows:
        print(f"  {human_size(size):>10}  {path}")
    policy_exit(args, total)


def cmd_stats(conn, args):
    row = conn.execute(QUERIES["summary"]).fetchone()
    n_content, t_bytes, n_inst = row or (0, 0, 0)
    n_media = conn.execute(QUERIES["media_count"]).fetchone()[0]
    dup = n_inst - n_content if n_content else 0
    if emit_json(args, {"media": n_media, "content": n_content,
                        "bytes": t_bytes, "instances": n_inst,
                        "duplicate_placements": dup}):
        return
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
    # One grouped pass for the medium list; the rest is already materialised.
    rows = conn.execute(
        "SELECT c.hash, GROUP_CONCAT(DISTINCT i.medium_id),"
        "       c.backup_copies, c.example_path"
        " FROM content c LEFT JOIN instances i ON i.hash=c.hash"
        " GROUP BY c.hash, c.backup_copies, c.example_path").fetchall()
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

def build_parser():
    p = argparse.ArgumentParser(
        prog="holdings",
        description="Catalog of which media hold which files."
                    " Placement truth in SQLite; semantics belong to OntoDAG.")
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"catalog database (default {DEFAULT_DB}); a path,"
                        f" or a published read-only catalog as a"
                        f" bzz://|bzzf:// URL (needs holdings[swarm])")
    p.add_argument("--max-scan-age", type=float,
                   default=DEFAULT_MAX_SCAN_AGE_DAYS, metavar="DAYS",
                   help="warn when a published catalog's newest scan is older"
                        f" than this (default {DEFAULT_MAX_SCAN_AGE_DAYS:.0f};"
                        " 0 disables)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def reads(name, **kw):
        """A read command: every one of them can speak JSON."""
        sp = sub.add_parser(name, **kw)
        sp.add_argument("--json", action="store_true",
                        help="emit one JSON object instead of a report")
        return sp

    s = sub.add_parser("add-medium", help="register a medium")
    s.add_argument("medium_id")
    s.add_argument("--kind", required=True,
                   choices=["drive", "laptop", "phone", "restic-repo",
                            "cloud", "other"])
    s.add_argument("--location", help="where it physically lives")
    s.add_argument("--durability", choices=sorted(DURABILITY),
                   help="what this copy survives; "
                        + "; ".join(f"{k}: {v.split(' (')[0]}"
                                    for k, v in DURABILITY.items()))
    s.add_argument("--site", metavar="NAME",
                   help="where this medium physically is, as a name you reuse"
                        " ('home', 'budapest'). Media sharing a site are"
                        " assumed to fail together")
    s.add_argument("--lease-expires", metavar="WHEN",
                   help="for --durability leased: YYYY-MM-DD, or a duration"
                        " from now such as 30d or 4w")
    s.add_argument("--backup", action="store_true",
                   help="shorthand for --durability independent")
    s.add_argument("--notes")
    s.set_defaults(func=cmd_add_medium, writes=True)

    s = reads("media", help="list media")
    s.set_defaults(func=cmd_media, writes=False)

    s = sub.add_parser("scan", help="scan a mounted medium (or subtree)")
    s.add_argument("medium_id")
    s.add_argument("mount_path")
    s.add_argument("--root", help="only scan this subtree (relative)")
    s.add_argument("--exclude-file", help="extra exclude patterns, one per line")
    s.add_argument("--full", action="store_true",
                   help="rehash everything (ignore mtime+size shortcut)")
    s.set_defaults(func=cmd_scan, writes=True)

    s = sub.add_parser("import-restic",
                       help="ingest `restic ls --json` output ('-' = stdin)")
    s.add_argument("medium_id")
    s.add_argument("listing")
    s.set_defaults(func=cmd_import_restic, writes=True)

    s = reads("whereis", help="which media hold this file?")
    s.add_argument("target", help="path, filename, or sha256:... hash")
    s.set_defaults(func=cmd_whereis, writes=False)

    s = reads("redundancy", help="content below N backup copies")
    s.add_argument("--min-copies", type=int, default=2)
    s.add_argument("--min-sites", type=int, default=1, metavar="N",
                   help="the '1 offsite' of 3-2-1: require backup copies at"
                        " N distinct sites (default 1, i.e. unchecked)")
    s.add_argument("--min-kinds", type=int, default=1, metavar="N",
                   help="the '2 media' of 3-2-1: require backup copies on N"
                        " distinct kinds of medium (default 1, unchecked)")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--lease-margin", type=float, metavar="DAYS",
                   default=DEFAULT_LEASE_MARGIN_DAYS,
                   help="a leased medium with less than this left is reported"
                        f" as at risk (default {DEFAULT_LEASE_MARGIN_DAYS:.0f})")
    s.add_argument("--exit-code", action="store_true",
                   help="exit 1 when anything is below --min-copies or a"
                        " leased copy is at risk, so the 3-2-1 policy can be"
                        " enforced from cron")
    s.set_defaults(func=cmd_redundancy, writes=False)

    s = reads("diff", help="on A but not on B")
    s.add_argument("medium_a")
    s.add_argument("medium_b")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_diff, writes=False)

    s = reads("only-on", help="content that exists ONLY on this medium")
    s.add_argument("medium_id")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--exit-code", action="store_true",
                   help="exit 1 while anything still exists only here --"
                        " the gate to put in front of wiping the drive")
    s.set_defaults(func=cmd_only_on, writes=False)

    s = reads("stats", help="catalog totals")
    s.set_defaults(func=cmd_stats, writes=False)

    s = reads("check",
                       help="verify derived columns against the base tables")
    s.set_defaults(func=cmd_check, writes=False)

    s = sub.add_parser("project-ontodag",
                       help="emit sys: placement projection (JSON lines)")
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_project_ontodag, writes=False)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = open_catalog(args.db, writes=args.writes, cmd=args.cmd,
                        max_scan_age=args.max_scan_age)
    try:
        args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
