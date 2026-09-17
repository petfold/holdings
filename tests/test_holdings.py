"""Tests for holdings — the placement catalog.

Driven through `main(argv)` wherever a command is under test, so the CLI
surface is exercised rather than the internals only. The design contract in
the README is what these pin: content hash is identity, everything is
regenerable by re-scanning, rescans do not rehash unchanged files, and the
observer never writes to the data it catalogs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import holdings  # noqa: E402


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def db(tmp_path):
    """A catalog database path (created on first connect)."""
    return str(tmp_path / "catalog.sqlite")


@pytest.fixture
def run(db):
    """Invoke the CLI with --db pointed at the test database."""
    def _run(*argv):
        return holdings.main(["--db", db, *argv])
    return _run


@pytest.fixture
def conn(db):
    def _open():
        return sqlite3.connect(db)
    return _open


def write(path: Path, content: bytes | str = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


# ------------------------------------------------------------------ hashing

def test_hash_is_content_addressed(tmp_path):
    """Same bytes, different names and directories — one identity."""
    a = write(tmp_path / "a.txt", "hello")
    b = write(tmp_path / "deep" / "b.dat", "hello")
    c = write(tmp_path / "c.txt", "different")
    assert holdings.sha256_file(a) == holdings.sha256_file(b)
    assert holdings.sha256_file(a) != holdings.sha256_file(c)


def test_hash_carries_its_algorithm(tmp_path):
    h = holdings.sha256_file(write(tmp_path / "f", "x"))
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_hash_is_streamed_not_slurped(tmp_path):
    """A buffer smaller than the file must not change the answer."""
    f = write(tmp_path / "big", b"y" * 5000)
    assert holdings.sha256_file(f, bufsize=7) == holdings.sha256_file(f)


# ----------------------------------------------------------------- excludes

def test_default_excludes_cover_the_usual_noise():
    pats = holdings.load_excludes(None)
    for name in (".git", "node_modules", ".cache", ".DS_Store"):
        assert holdings.is_excluded(name, name, pats), name


def test_exclude_matches_glob_and_full_relative_path():
    pats = holdings.load_excludes(None)
    assert holdings.is_excluded("x/scratch.tmp", "scratch.tmp", pats)
    assert not holdings.is_excluded("notes/keep.txt", "keep.txt", pats)


def test_exclude_file_extends_the_defaults(tmp_path):
    f = write(tmp_path / "ex", "# a comment\n*.iso\n\nprivate\n")
    pats = holdings.load_excludes(str(f))
    assert holdings.is_excluded("d/x.iso", "x.iso", pats)
    assert holdings.is_excluded("private", "private", pats)
    assert "# a comment" not in pats      # comments and blanks are dropped
    assert ".git" in pats                 # defaults survive


# ------------------------------------------------------------------- schema

def test_connect_creates_the_schema_and_is_idempotent(db):
    holdings.db_connect(db).close()
    holdings.db_connect(db).close()            # again: must not fail
    c = sqlite3.connect(db)
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"media", "content", "instances", "scans"} <= tables


# -------------------------------------------------------------------- media

def test_add_medium_then_list(run, conn, capsys):
    run("add-medium", "laptop-x1", "--kind", "laptop", "--location", "with me")
    row = conn().execute("SELECT kind, location_hint, is_backup FROM media").fetchone()
    assert row == ("laptop", "with me", 0)
    run("media")
    assert "laptop-x1" in capsys.readouterr().out


def test_backup_flag_is_recorded(run, conn):
    run("add-medium", "drive", "--kind", "drive", "--backup")
    assert conn().execute("SELECT is_backup FROM media").fetchone()[0] == 1


def test_re_adding_a_medium_updates_it(run, conn):
    run("add-medium", "m", "--kind", "drive")
    run("add-medium", "m", "--kind", "cloud", "--notes", "moved")
    rows = conn().execute("SELECT kind, notes FROM media").fetchall()
    assert rows == [("cloud", "moved")]       # replaced, not duplicated


# --------------------------------------------------------------------- scan

def test_scan_records_content_and_instances(run, conn, tmp_path):
    root = tmp_path / "disk"
    write(root / "a.txt", "one")
    write(root / "sub" / "b.txt", "two")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    c = conn()
    assert c.execute("SELECT COUNT(*) FROM content").fetchone()[0] == 2
    paths = {r[0] for r in c.execute("SELECT path FROM instances")}
    assert paths == {"a.txt", os.path.join("sub", "b.txt")}


def test_identical_files_share_one_content_row(run, conn, tmp_path):
    """Dedup is free: two placements, one identity."""
    root = tmp_path / "disk"
    write(root / "one.txt", "same")
    write(root / "copy" / "two.txt", "same")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    c = conn()
    assert c.execute("SELECT COUNT(*) FROM content").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM instances").fetchone()[0] == 2


def test_rescan_does_not_rehash_unchanged_files(run, conn, tmp_path, capsys):
    """The README's claim: unchanged files are recognised by size+mtime."""
    root = tmp_path / "disk"
    write(root / "a.txt", "one")
    write(root / "b.txt", "two")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    capsys.readouterr()
    run("scan", "d", str(root))
    hashed = conn().execute(
        "SELECT hashed FROM scans ORDER BY scan_id DESC LIMIT 1").fetchone()[0]
    assert hashed == 0


def test_full_forces_rehash(run, conn, tmp_path):
    root = tmp_path / "disk"
    write(root / "a.txt", "one")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    run("scan", "d", str(root), "--full")
    hashed = conn().execute(
        "SELECT hashed FROM scans ORDER BY scan_id DESC LIMIT 1").fetchone()[0]
    assert hashed == 1


def test_changed_content_gets_a_new_hash(run, conn, tmp_path):
    root = tmp_path / "disk"
    f = write(root / "a.txt", "before")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    first = conn().execute("SELECT hash FROM instances").fetchone()[0]
    time.sleep(0.01)
    write(f, "after and longer")
    run("scan", "d", str(root))
    c = conn()
    assert c.execute("SELECT hash FROM instances").fetchone()[0] != first
    # the old identity is kept in content — history is not rewritten
    assert c.execute("SELECT COUNT(*) FROM content").fetchone()[0] == 2


def test_vanished_files_are_pruned(run, conn, tmp_path):
    root = tmp_path / "disk"
    write(root / "stays.txt", "a")
    gone = write(root / "goes.txt", "b")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    gone.unlink()
    run("scan", "d", str(root))
    paths = {r[0] for r in conn().execute("SELECT path FROM instances")}
    assert paths == {"stays.txt"}


def test_excluded_directories_are_pruned(run, conn, tmp_path):
    root = tmp_path / "disk"
    write(root / "keep.txt", "k")
    write(root / ".git" / "config", "g")
    write(root / "node_modules" / "dep" / "index.js", "j")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    paths = {r[0] for r in conn().execute("SELECT path FROM instances")}
    assert paths == {"keep.txt"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinks_are_skipped(run, conn, tmp_path):
    """The README says symlinks are skipped — by design, not oversight."""
    root = tmp_path / "disk"
    real = write(root / "real.txt", "r")
    (root / "link.txt").symlink_to(real)
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    paths = {r[0] for r in conn().execute("SELECT path FROM instances")}
    assert paths == {"real.txt"}


def test_scan_of_a_subtree_leaves_the_rest_alone(run, conn, tmp_path):
    root = tmp_path / "disk"
    write(root / "top.txt", "t")
    write(root / "sub" / "inner.txt", "i")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    write(root / "sub" / "added.txt", "a")
    run("scan", "d", str(root), "--root", "sub")
    paths = {r[0] for r in conn().execute("SELECT path FROM instances")}
    assert "top.txt" in paths                      # untouched by the subtree scan
    assert os.path.join("sub", "added.txt") in paths


def test_scan_refuses_an_unregistered_medium(run, tmp_path):
    with pytest.raises(SystemExit):
        run("scan", "nope", str(tmp_path))


def test_scan_refuses_a_path_that_is_not_a_directory(run, tmp_path):
    f = write(tmp_path / "a_file", "x")
    run("add-medium", "d", "--kind", "drive")
    with pytest.raises(SystemExit):
        run("scan", "d", str(f))


def test_scan_never_writes_to_the_scanned_tree(run, tmp_path):
    """Observer, not authority: the tree must come back byte-identical."""
    root = tmp_path / "disk"
    write(root / "a.txt", "one")
    write(root / "sub" / "b.txt", "two")
    before = {p.relative_to(root).as_posix(): (p.stat().st_size, p.read_bytes())
              for p in root.rglob("*") if p.is_file()}
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    after = {p.relative_to(root).as_posix(): (p.stat().st_size, p.read_bytes())
             for p in root.rglob("*") if p.is_file()}
    assert before == after


# ----------------------------------------------------------------- resolve

def test_resolve_by_exact_path_and_by_basename(run, db, tmp_path):
    root = tmp_path / "disk"
    write(root / "sub" / "unique.txt", "u")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    c = holdings.db_connect(db)
    try:
        by_path = holdings.resolve_hash(c, os.path.join("sub", "unique.txt"))
        by_base = holdings.resolve_hash(c, "unique.txt")
        assert by_path == by_base is not None
        assert holdings.resolve_hash(c, "sha256:abc") == "sha256:abc"
        assert holdings.resolve_hash(c, "nothing-like-this") is None
    finally:
        c.close()


def test_ambiguous_basename_refuses_to_guess(run, db, tmp_path):
    """Two different files, same name: the tool must not pick one.

    Since the basename lookup moved onto `instances.name`, this also pins
    that the index did not cost the ambiguity check.
    """
    root = tmp_path / "disk"
    write(root / "one" / "notes.txt", "first")
    write(root / "two" / "notes.txt", "second")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    c = holdings.db_connect(db)
    try:
        with pytest.raises(SystemExit):
            holdings.resolve_hash(c, "notes.txt")
    finally:
        c.close()


def test_whereis_lists_every_medium_holding_it(run, capsys, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "shared.txt", "same")
    write(b / "elsewhere" / "shared.txt", "same")
    run("add-medium", "laptop", "--kind", "laptop")
    run("add-medium", "drive", "--kind", "drive", "--backup")
    run("scan", "laptop", str(a))
    run("scan", "drive", str(b))
    capsys.readouterr()
    run("whereis", "shared.txt")
    out = capsys.readouterr().out
    assert "present on 2 media (1 backup)" in out
    assert "laptop" in out and "drive" in out


# -------------------------------------------------- match_known_content

def test_unique_basename_and_size_matches(run, db, tmp_path):
    root = tmp_path / "disk"
    write(root / "sub" / "report.pdf", "abcdef")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    c = holdings.db_connect(db)
    try:
        h = holdings.match_known_content(c, "/backup/sub/report.pdf", 6)
        assert h is not None and h.startswith("sha256:")
    finally:
        c.close()


def test_ambiguous_match_returns_none(run, db, tmp_path):
    """Conservative by design: ambiguity is recorded, never resolved by luck."""
    root = tmp_path / "disk"
    write(root / "x" / "same.bin", "aaaaaa")      # same size, different content
    write(root / "y" / "same.bin", "bbbbbb")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    c = holdings.db_connect(db)
    try:
        assert holdings.match_known_content(c, "/b/same.bin", 6) is None
    finally:
        c.close()


def test_size_mismatch_is_not_a_match(run, db, tmp_path):
    root = tmp_path / "disk"
    write(root / "f.txt", "abc")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    c = holdings.db_connect(db)
    try:
        assert holdings.match_known_content(c, "/elsewhere/f.txt", 999) is None
    finally:
        c.close()


# ------------------------------------------------------------ import-restic

def test_import_restic_counts_a_backup_copy(run, conn, capsys, tmp_path):
    root = tmp_path / "disk"
    write(root / "doc.txt", "content!")           # 8 bytes
    run("add-medium", "laptop", "--kind", "laptop")
    run("scan", "laptop", str(root))
    run("add-medium", "restic", "--kind", "restic-repo", "--backup")

    listing = write(tmp_path / "ls.json", json.dumps(
        {"struct_type": "node", "type": "file",
         "path": "/home/p/doc.txt", "size": 8}) + "\n")
    capsys.readouterr()
    run("import-restic", "restic", str(listing))

    c = conn()
    media = {r[0] for r in c.execute(
        "SELECT medium_id FROM instances WHERE hash LIKE 'sha256:%'")}
    assert media == {"laptop", "restic"}          # matched to known content


def test_import_restic_marks_unmatched_as_unverified(run, conn, capsys, tmp_path):
    run("add-medium", "restic", "--kind", "restic-repo", "--backup")
    listing = write(tmp_path / "ls.json", json.dumps(
        {"struct_type": "node", "type": "file",
         "path": "/unknown/thing.bin", "size": 4242}) + "\n")
    capsys.readouterr()
    run("import-restic", "restic", str(listing))
    h = conn().execute("SELECT hash FROM instances").fetchone()[0]
    assert h.startswith("unverified:")


# ------------------------------------------------------- redundancy reports

def test_redundancy_flags_content_with_no_backup(run, capsys, tmp_path):
    root = tmp_path / "disk"
    write(root / "precious.txt", "only copy")
    run("add-medium", "laptop", "--kind", "laptop")
    run("scan", "laptop", str(root))
    capsys.readouterr()
    run("redundancy")
    assert "precious.txt" in capsys.readouterr().out


def test_redundancy_is_quiet_when_everything_is_backed_up(run, capsys, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "f.txt", "same")
    write(b / "f.txt", "same")
    run("add-medium", "laptop", "--kind", "laptop")
    run("add-medium", "drive", "--kind", "drive", "--backup")
    run("scan", "laptop", str(a))
    run("scan", "drive", str(b))
    capsys.readouterr()
    run("redundancy", "--min-copies", "1")
    assert "OK:" in capsys.readouterr().out


def test_only_on_is_the_danger_list(run, capsys, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "shared.txt", "shared")
    write(a / "alone.txt", "alone")
    write(b / "shared.txt", "shared")
    run("add-medium", "laptop", "--kind", "laptop")
    run("add-medium", "drive", "--kind", "drive", "--backup")
    run("scan", "laptop", str(a))
    run("scan", "drive", str(b))
    capsys.readouterr()
    run("only-on", "laptop")
    out = capsys.readouterr().out
    assert "alone.txt" in out
    assert "shared.txt" not in out


def test_diff_compares_by_content_not_by_path(run, capsys, tmp_path):
    """Same bytes under a different path count as present on both."""
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "here.txt", "same")
    write(a / "extra.txt", "extra")
    write(b / "renamed" / "there.txt", "same")
    run("add-medium", "one", "--kind", "drive")
    run("add-medium", "two", "--kind", "drive")
    run("scan", "one", str(a))
    run("scan", "two", str(b))
    capsys.readouterr()
    run("diff", "one", "two")
    out = capsys.readouterr().out
    assert "extra.txt" in out
    assert "here.txt" not in out


def test_stats_counts_media_content_and_instances(run, capsys, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "f.txt", "same")
    write(b / "f.txt", "same")
    run("add-medium", "one", "--kind", "drive")
    run("add-medium", "two", "--kind", "drive")
    run("scan", "one", str(a))
    run("scan", "two", str(b))
    capsys.readouterr()
    run("stats")
    out = capsys.readouterr().out
    assert "media: 2" in out
    assert "unique content: 1" in out
    assert "instances: 2" in out


# ------------------------------------------------------------- projection

def test_projection_emits_one_line_per_content_object(run, capsys, tmp_path):
    root = tmp_path / "disk"
    write(root / "notes.md", "m")
    write(root / "photo.JPG", "p")
    run("add-medium", "laptop", "--kind", "laptop")
    run("scan", "laptop", str(root))
    capsys.readouterr()
    run("project-ontodag")
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2
    for rec in lines:
        assert rec["item"].startswith("sha256:")
        assert "sys:on:laptop" in rec["supercategories"]


def test_projection_lowercases_the_type_and_counts_backups(run, capsys, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "photo.JPG", "p")
    write(b / "photo.JPG", "p")
    run("add-medium", "laptop", "--kind", "laptop")
    run("add-medium", "drive", "--kind", "drive", "--backup")
    run("scan", "laptop", str(a))
    run("scan", "drive", str(b))
    capsys.readouterr()
    run("project-ontodag")
    rec = json.loads(capsys.readouterr().out.splitlines()[0])
    assert "sys:type:jpg" in rec["supercategories"]     # lowercased
    assert "sys:backup:1" in rec["supercategories"]     # one backup medium


def test_projection_is_regenerable_and_stable(run, capsys, tmp_path):
    """`sys:` output is a cache: the same catalog projects identically."""
    root = tmp_path / "disk"
    write(root / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    capsys.readouterr()
    run("project-ontodag")
    first = capsys.readouterr().out
    run("project-ontodag")
    assert capsys.readouterr().out == first


def test_projection_writes_to_a_file_when_asked(run, capsys, tmp_path):
    root = tmp_path / "disk"
    write(root / "a.txt", "a")
    out = tmp_path / "proj.jsonl"
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    run("project-ontodag", "--out", str(out))
    assert json.loads(out.read_text().splitlines()[0])["item"].startswith("sha256:")


# -------------------------------------------------------------- formatting

@pytest.mark.parametrize("n,expected", [
    (0, "0B"), (None, "0B"), (999, "999B"), (2048, "2.0KB"),
    (5 * 1024 ** 3, "5.0GB"),
])
def test_human_size(n, expected):
    """None reads as zero rather than raising — the reports pass it straight
    from a SUM() that can come back NULL."""
    assert holdings.human_size(n) == expected


def test_human_size_keeps_bytes_whole_and_scales_with_one_decimal():
    assert holdings.human_size(1023) == "1023B"      # no decimal below 1 KB
    assert holdings.human_size(1024) == "1.0KB"      # one decimal above


# --------------------------------------------------- published catalogs (opt)

# The optional read path: a catalog published read-only and opened by URL.
# What holdings owns here is the dispatch — which paths are URLs, which
# commands may run against one, and what someone without the extra is told.
# The reader itself is swarmlite's.

def fake_swarmlite(monkeypatch, db_path):
    """Stand in for the extra, so the dispatch is testable without it.

    `connect` returns a genuinely read-only sqlite3 connection over the
    local file, which is the contract holdings relies on, and records the
    URL it was asked for.
    """
    import types
    seen = {}

    def connect(url, **kwargs):
        seen["url"] = url
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    monkeypatch.setitem(sys.modules, "swarmlite",
                        types.SimpleNamespace(connect=connect))
    return seen


def no_swarmlite(monkeypatch):
    """Simulate the extra not being installed, whether or not it is."""
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "swarmlite":
            raise ModuleNotFoundError("No module named 'swarmlite'",
                                      name="swarmlite")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)


@pytest.mark.parametrize("db,remote", [
    ("bzz://abc/catalog.sqlite", True),
    ("bzzf://owner/topic/catalog.sqlite", True),
    ("file:///srv/catalog.sqlite", True),
    ("memory://catalog.sqlite", True),
    ("/home/peter/catalog.sqlite", False),
    ("catalog.sqlite", False),
    ("./bzz/catalog.sqlite", False),        # a directory named bzz is a path
    ("~/Sync/catalog/catalog.sqlite", False),
])
def test_only_url_forms_are_treated_as_published_catalogs(db, remote):
    assert holdings.is_remote_url(db) is remote


def test_every_command_declares_whether_it_writes():
    """A new subcommand must say which side of the line it is on.

    Without this, one added later inherits no `writes` default and fails at
    dispatch — or worse, is quietly allowed to write to a read-only catalog.
    """
    import argparse
    p = holdings.build_parser()
    [subs] = [a for a in p._actions
              if isinstance(a, argparse._SubParsersAction)]
    undeclared = [name for name, sp in subs.choices.items()
                  if sp.get_default("writes") is None]
    assert not undeclared, f"commands not declaring writes=: {undeclared}"


def test_the_commands_that_write_are_the_ones_that_change_placement():
    """Pins the split itself, so a read command cannot silently become one
    that writes (which would then be refused against a published catalog)."""
    import argparse
    [subs] = [a for a in holdings.build_parser()._actions
              if isinstance(a, argparse._SubParsersAction)]
    writers = {n for n, sp in subs.choices.items() if sp.get_default("writes")}
    assert writers == {"add-medium", "scan", "import-restic", "import-swarm",
                       "check-swarm"}


@pytest.mark.parametrize("argv", [
    ["add-medium", "d", "--kind", "drive"],
    ["scan", "d", "/tmp"],
    ["import-restic", "r", "-"],
])
def test_writing_commands_refuse_a_published_catalog(argv, capsys, monkeypatch):
    """Published catalogs are read-only; the error points at the real route."""
    fake_swarmlite(monkeypatch, "/nonexistent.sqlite")
    with pytest.raises(SystemExit) as e:
        holdings.main(["--db", "bzzf://owner/holdings/catalog.sqlite", *argv])
    msg = str(e.value)
    assert "read-only" in msg and "swarmlite publish" in msg


def test_reading_a_published_catalog_without_the_extra_says_how_to_install(
        monkeypatch):
    no_swarmlite(monkeypatch)
    with pytest.raises(SystemExit) as e:
        holdings.main(["--db", "bzz://deadbeef/catalog.sqlite", "stats"])
    assert "holdings[swarm]" in str(e.value)


def test_a_local_catalog_never_imports_the_extra(run, monkeypatch, tmp_path):
    """The contract: stdlib only unless you explicitly ask for a URL."""
    no_swarmlite(monkeypatch)
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    run("stats")            # would raise if the extra were on the local path


def test_read_commands_work_against_a_published_catalog(db, run, monkeypatch,
                                                        tmp_path, capsys):
    """The catalog is built locally, then read back through the URL path."""
    root = tmp_path / "disk"
    write(root / "holiday.jpg", "photo")
    run("add-medium", "drive-budapest", "--kind", "drive", "--backup")
    run("scan", "drive-budapest", str(root))
    capsys.readouterr()

    url = "bzzf://owner/holdings/catalog.sqlite"
    seen = fake_swarmlite(monkeypatch, db)
    holdings.main(["--db", url, "whereis", "holiday.jpg"])
    out = capsys.readouterr().out
    assert seen["url"] == url          # dispatched, not opened as a path
    assert "drive-budapest" in out


def test_file_url_over_a_live_catalog_warns_that_the_wal_is_invisible(
        db, run, monkeypatch, tmp_path, capsys):
    """Measured against real swarmlite: its read-only VFS reports the WAL
    absent, so an un-checkpointed write is silently missing. Right for a
    published artifact (publishing checkpoints first), wrong to pass on in
    silence for a `file://` read of a live catalog."""
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    Path(db + "-wal").write_bytes(b"\x00" * 64)     # a writer's sidecar
    capsys.readouterr()

    fake_swarmlite(monkeypatch, db)
    holdings.main(["--db", f"file://{db}", "stats"])
    assert "WAL are invisible" in capsys.readouterr().err


def test_no_wal_warning_when_there_is_nothing_uncheckpointed(
        db, run, monkeypatch, tmp_path, capsys):
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    capsys.readouterr()

    fake_swarmlite(monkeypatch, db)
    holdings.main(["--db", f"file://{db}", "stats"])
    assert capsys.readouterr().err == ""


# ------------------------------------------------------- basename lookups

OLD_INSTANCES = """
CREATE TABLE media (medium_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
    location_hint TEXT, is_backup INTEGER NOT NULL DEFAULT 0, notes TEXT,
    last_scanned REAL);
CREATE TABLE content (hash TEXT PRIMARY KEY, size INTEGER NOT NULL,
    first_seen REAL NOT NULL);
CREATE TABLE instances (medium_id TEXT NOT NULL, path TEXT NOT NULL,
    hash TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL,
    seen_at REAL NOT NULL, PRIMARY KEY (medium_id, path));
"""


def test_basename_of_a_stored_path():
    assert holdings.basename("archive/2019/holiday.jpg") == "holiday.jpg"
    assert holdings.basename("holiday.jpg") == "holiday.jpg"
    assert holdings.basename("a/b/") == ""


def test_whereis_by_bare_filename_uses_the_name_index(db, run, tmp_path):
    """The lookup `whereis holiday.jpg` performs — the README's own example.

    It used to be `path LIKE '%/name'`, which a leading wildcard makes
    unservable by any index: measured at 8 minutes unfinished over the
    network on a 125 MB catalog.

    Asserted at a size where the index is genuinely the cheaper plan. On a
    handful of rows a scan is cheaper and SQLite rightly picks one, so a
    plan assertion on a tiny catalog would be testing the planner's
    arithmetic rather than this schema.
    """
    write(tmp_path / "disk" / "deep" / "holiday.jpg", "photo")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    with sqlite3.connect(db) as c:
        c.executemany(
            "INSERT INTO instances"
            " (medium_id, path, name, hash, size, seen_at)"
            " VALUES ('d', ?, ?, 'sha256:aa', 1, 0)",
            [(f"bulk/{i}/f{i}.txt", f"f{i}.txt") for i in range(2000)])
        c.execute("ANALYZE")
        plan = c.execute("EXPLAIN QUERY PLAN SELECT DISTINCT hash FROM"
                         " instances WHERE name=? LIMIT 2", ("x",)).fetchall()
    assert "idx_instances_name" in plan[0][-1], plan


def test_same_name_same_content_is_not_ambiguous(run, capsys, tmp_path):
    write(tmp_path / "disk" / "a" / "notes.txt", "same")
    write(tmp_path / "disk" / "b" / "notes.txt", "same")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    capsys.readouterr()
    run("whereis", "notes.txt")                 # one hash, two placements
    assert "present on 1 media" in capsys.readouterr().out


def test_an_older_catalog_is_migrated_in_place_not_discarded(tmp_path, capsys):
    """A catalog holds facts about media that cannot be rescanned on demand —
    a drive in a safe in another country — so migration is additive."""
    db = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(db)
    old.executescript(OLD_INSTANCES)
    old.execute("INSERT INTO media (medium_id, kind, is_backup)"
                " VALUES ('drive-budapest','drive',1)")
    old.execute("INSERT INTO content VALUES ('sha256:aa', 10, 0)")
    old.execute("INSERT INTO instances (medium_id, path, hash, size, seen_at)"
                " VALUES ('drive-budapest','archive/2019/holiday.jpg',"
                "'sha256:aa',10,0)")
    old.commit()
    old.close()

    conn = holdings.db_connect(db)              # migrates on open
    assert conn.execute("SELECT name FROM instances").fetchone()[0] \
        == "holiday.jpg"
    # and the irreplaceable part is still there
    assert conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0] == 1
    conn.close()

    holdings.main(["--db", db, "whereis", "holiday.jpg"])
    assert "drive-budapest" in capsys.readouterr().out


def test_migration_is_idempotent(tmp_path):
    db = str(tmp_path / "old.sqlite")
    c = sqlite3.connect(db)
    c.executescript(OLD_INSTANCES)
    c.commit()
    c.close()
    for _ in range(3):                          # opening repeatedly is safe
        holdings.db_connect(db).close()
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(instances)")]
    assert cols.count("name") == 1


# ----------------------------------------------------------- derived state

# `instances.name` is a cache over `instances.path`. These pin that it cannot
# drift unnoticed, because the columns it will grow in future answer "do I
# have a backup of this?" and a stale yes is the answer that gets a drive
# wiped.

def test_check_reports_a_consistent_catalog(run, capsys, tmp_path):
    write(tmp_path / "disk" / "sub" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    capsys.readouterr()
    run("check")
    assert "consistent" in capsys.readouterr().out


def test_check_detects_drift_and_does_not_pretend_otherwise(run, db, capsys,
                                                            tmp_path):
    write(tmp_path / "disk" / "sub" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    with sqlite3.connect(db) as c:                  # corrupt the cache
        c.execute("UPDATE instances SET name='wrong.txt'")
    with pytest.raises(SystemExit) as e:
        run("check")
    assert "cache" in str(e.value)
    assert "DRIFT" in capsys.readouterr().err


def test_check_is_read_only_so_it_works_on_a_published_catalog():
    import argparse
    [subs] = [a for a in holdings.build_parser()._actions
              if isinstance(a, argparse._SubParsersAction)]
    assert subs.choices["check"].get_default("writes") is False


def test_derived_state_survives_an_arbitrary_sequence_of_writes(
        db, run, tmp_path, capsys):
    """The property that matters: after any mix of write commands, stored
    derived state equals a fresh recomputation."""
    import random
    random.seed(11)
    root = tmp_path / "disk"
    run("add-medium", "d", "--kind", "drive")
    run("add-medium", "e", "--kind", "drive", "--backup")

    for step in range(12):
        action = random.choice(["add", "rename", "delete", "rescan", "medium"])
        if action == "add":
            depth = random.randint(0, 3)
            d = root.joinpath(*[f"lvl{i}" for i in range(depth)])
            write(d / f"f{step}.txt", f"content-{step}")
        elif action == "rename":
            files = sorted(root.rglob("*.txt"))
            if files:
                f = random.choice(files)
                f.rename(f.with_name(f"renamed{step}.txt"))
        elif action == "delete":
            files = sorted(root.rglob("*.txt"))
            if files:
                random.choice(files).unlink()
        elif action == "medium":
            run("add-medium", random.choice("de"), "--kind", "drive",
                "--backup")
        root.mkdir(parents=True, exist_ok=True)
        run("scan", random.choice("de"), str(root))
        capsys.readouterr()

        # A raw connection on purpose: db_connect() migrates, which
        # backfills NULL names and would repair a writer bug before the
        # assertion could see it.
        conn = sqlite3.connect(db)
        try:
            assert holdings.derived_drift(conn) == [], f"drift at step {step}"
        finally:
            conn.close()


def test_import_restic_also_keeps_derived_state_consistent(db, run, tmp_path,
                                                           capsys):
    listing = tmp_path / "ls.json"
    listing.write_text("\n".join([
        json.dumps({"struct_type": "node", "type": "file",
                    "path": "/deep/nested/report.pdf", "size": 120}),
        json.dumps({"struct_type": "node", "type": "file",
                    "path": "/top.txt", "size": 4}),
    ]))
    run("add-medium", "r", "--kind", "restic-repo", "--backup")
    run("import-restic", "r", str(listing))
    capsys.readouterr()
    conn = sqlite3.connect(db)          # raw: see what the writer wrote
    try:
        assert holdings.derived_drift(conn) == []
        names = {r[0] for r in conn.execute("SELECT name FROM instances")}
        assert names == {"report.pdf", "top.txt"}
    finally:
        conn.close()


# --------------------------------------------- materialised copy counts

def test_flipping_backup_on_a_medium_updates_every_copy_count(run, db, capsys,
                                                              tmp_path):
    """The reason refresh runs after add-medium and not only after scan:
    one flag on one medium changes backup_copies for everything on it."""
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")          # not a backup yet
    run("scan", "d", str(tmp_path / "disk"))
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT backup_copies FROM content").fetchone()[0] == 0

    run("add-medium", "d", "--kind", "drive", "--backup")
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT backup_copies FROM content").fetchone()[0] == 1
    capsys.readouterr()
    run("redundancy", "--min-copies", "1")
    assert "OK" in capsys.readouterr().out


def test_re_adding_a_medium_does_not_lose_its_totals(run, db, tmp_path,
                                                     capsys):
    """add-medium is INSERT OR REPLACE, which blanks the derived columns on
    the row; the refresh afterwards has to put them back."""
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    run("add-medium", "d", "--kind", "drive", "--location", "moved house")
    capsys.readouterr()
    run("media")
    out = capsys.readouterr().out
    assert "moved house" in out
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT file_count FROM media").fetchone()[0] == 1
        assert holdings.derived_drift(c) == []


def test_only_on_still_finds_content_held_nowhere_else(run, capsys, tmp_path):
    """Behaviour pinned across the rewrite onto content.copies."""
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "shared.txt", "shared")
    write(b / "shared.txt", "shared")
    write(a / "only-here.txt", "unique")
    run("add-medium", "a", "--kind", "drive")
    run("add-medium", "b", "--kind", "drive")
    run("scan", "a", str(a))
    run("scan", "b", str(b))
    capsys.readouterr()
    run("only-on", "a")
    out = capsys.readouterr().out
    assert "only-here.txt" in out and "shared.txt" not in out
    assert out.startswith("1 files")


def test_stale_copy_counts_are_reported_not_trusted(run, db, tmp_path,
                                                    capsys):
    """The dangerous direction: stored counts claiming backups that the base
    tables do not support."""
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    with sqlite3.connect(db) as c:
        c.execute("UPDATE content SET backup_copies=3, copies=3")
    with pytest.raises(SystemExit):
        run("check")
    assert "stale" in capsys.readouterr().err


def test_an_old_catalog_gets_counts_not_silent_zeroes(tmp_path, capsys):
    """Migration must refresh, not just add columns.

    A NOT NULL DEFAULT 0 column reads as "no backup copies anywhere", which
    would make `redundancy` denounce a perfectly backed-up catalog — and
    `only-on` is the list people wipe drives from.
    """
    db = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(db)
    old.executescript(OLD_INSTANCES)
    old.execute("INSERT INTO media (medium_id, kind, is_backup)"
                " VALUES ('drive-budapest','drive',1)")
    old.execute("INSERT INTO content VALUES ('sha256:aa', 10, 0)")
    old.execute("INSERT INTO instances (medium_id, path, hash, size, seen_at)"
                " VALUES ('drive-budapest','holiday.jpg','sha256:aa',10,0)")
    old.commit()
    old.close()

    conn = holdings.db_connect(db)
    try:
        assert conn.execute(
            "SELECT copies, backup_copies, example_path FROM content"
        ).fetchone() == (1, 1, "holiday.jpg")
        assert holdings.derived_drift(conn) == []
    finally:
        conn.close()

    holdings.main(["--db", db, "redundancy", "--min-copies", "1"])
    assert "OK" in capsys.readouterr().out


def test_only_here_flag_follows_content_becoming_redundant(run, db, capsys,
                                                           tmp_path):
    """`only-on` is the list people wipe drives from, so the flag that drives
    it has to clear the moment a second copy appears."""
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "precious.txt", "precious")
    run("add-medium", "a", "--kind", "drive")
    run("add-medium", "b", "--kind", "drive")
    run("scan", "a", str(a))
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT only_here FROM instances").fetchone()[0] == 1

    write(b / "precious.txt", "precious")        # now held twice
    run("scan", "b", str(b))
    with sqlite3.connect(db) as c:
        assert [r[0] for r in c.execute("SELECT only_here FROM instances")] \
            == [0, 0]
        assert holdings.derived_drift(c) == []
    capsys.readouterr()
    run("only-on", "a")
    assert capsys.readouterr().out.startswith("0 files")


def test_only_here_drift_is_detected(run, db, capsys, tmp_path):
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    with sqlite3.connect(db) as c:
        c.execute("UPDATE instances SET only_here=0")   # hides the danger
    with pytest.raises(SystemExit):
        run("check")
    assert "only_here" in capsys.readouterr().err


# ------------------------------------------------- published catalog age

def age_catalog(db, days):
    """Backdate every medium's last scan."""
    with sqlite3.connect(db) as c:
        c.execute("UPDATE media SET last_scanned=?",
                  (time.time() - days * 86400,))


def test_an_old_published_catalog_says_so(db, run, monkeypatch, tmp_path,
                                          capsys):
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    age_catalog(db, 200)
    capsys.readouterr()

    fake_swarmlite(monkeypatch, db)
    holdings.main(["--db", "bzzf://o/holdings/c.sqlite", "stats"])
    err = capsys.readouterr().err
    assert "has been scanned since" in err and "200 days ago" in err


def test_a_recent_published_catalog_is_quiet(db, run, monkeypatch, tmp_path,
                                             capsys):
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    age_catalog(db, 3)
    capsys.readouterr()

    fake_swarmlite(monkeypatch, db)
    holdings.main(["--db", "bzzf://o/holdings/c.sqlite", "stats"])
    assert capsys.readouterr().err == ""


def test_the_staleness_warning_can_be_silenced(db, run, monkeypatch, tmp_path,
                                               capsys):
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    age_catalog(db, 900)
    capsys.readouterr()

    fake_swarmlite(monkeypatch, db)
    holdings.main(["--db", "bzzf://o/holdings/c.sqlite",
                   "--max-scan-age", "0", "stats"])
    assert capsys.readouterr().err == ""


def test_a_local_catalog_is_not_nagged_about_its_age(db, run, tmp_path,
                                                     capsys):
    """Whoever holds the local file is the one who would rescan; the warning
    is for readers who cannot."""
    write(tmp_path / "disk" / "a.txt", "a")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(tmp_path / "disk"))
    age_catalog(db, 900)
    capsys.readouterr()
    run("stats")
    assert capsys.readouterr().err == ""


def test_an_empty_published_catalog_does_not_warn(db, monkeypatch, capsys):
    holdings.db_connect(db).close()              # schema only, no media
    fake_swarmlite(monkeypatch, db)
    holdings.main(["--db", "bzzf://o/holdings/c.sqlite", "stats"])
    assert capsys.readouterr().err == ""


# ------------------------------------------------- machine-readable output

# The alternative considered was an HTTP API. These pin the flag instead:
# no port, no process, no dependency in front of a file anyone can open.

READ_COMMANDS = ["media", "whereis", "redundancy", "diff", "only-on",
                 "stats", "check"]


@pytest.fixture
def stocked(run, tmp_path, capsys):
    """A catalog with a backed-up file and one held nowhere else."""
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "shared.txt", "shared")
    write(b / "shared.txt", "shared")
    write(a / "only-here.txt", "unique")
    run("add-medium", "a", "--kind", "drive", "--backup")
    run("add-medium", "b", "--kind", "drive", "--backup")
    run("scan", "a", str(a))
    run("scan", "b", str(b))
    capsys.readouterr()
    return run


def test_every_read_command_can_speak_json():
    """A command added later must not quietly be the one that cannot."""
    import argparse
    [subs] = [a for a in holdings.build_parser()._actions
              if isinstance(a, argparse._SubParsersAction)]
    readers = {n for n, sp in subs.choices.items()
               if sp.get_default("writes") is False}
    without = {n for n in readers
               if sp_has_no_json(subs.choices[n])} - {"project-ontodag"}
    assert not without, f"read commands without --json: {without}"


def sp_has_no_json(sp) -> bool:
    return not any(o == "--json" for a in sp._actions for o in a.option_strings)


@pytest.mark.parametrize("argv", [
    ["media"], ["stats"], ["check"], ["whereis", "shared.txt"],
    ["redundancy"], ["only-on", "a"], ["diff", "a", "b"],
])
def test_json_output_parses(stocked, capsys, argv):
    stocked(*argv, "--json")
    out = capsys.readouterr().out
    assert json.loads(out)          # one object, one line
    assert out.count("\n") == 1


def test_whereis_json_carries_the_placements(stocked, capsys):
    stocked("whereis", "shared.txt", "--json")
    doc = json.loads(capsys.readouterr().out)
    assert doc["hash"].startswith("sha256:")
    assert doc["copies"] == 2 and doc["backup_copies"] == 2
    assert {p["medium_id"] for p in doc["placements"]} == {"a", "b"}


def test_only_on_json_matches_the_report(stocked, capsys):
    stocked("only-on", "a", "--json")
    doc = json.loads(capsys.readouterr().out)
    assert doc["files"] == 1
    assert doc["items"][0]["path"] == "only-here.txt"


def test_redundancy_exit_code_enforces_the_policy(stocked):
    """The README has called this a checkable report since v0.1; until now
    it exited 0 whatever it found, so nothing could check it."""
    stocked("redundancy", "--min-copies", "2")          # no flag: still 0
    with pytest.raises(SystemExit) as e:
        stocked("redundancy", "--min-copies", "3", "--exit-code")
    assert e.value.code == 1


def test_redundancy_exit_code_is_quiet_when_the_policy_holds(stocked):
    """Everything here has at least one backup copy, so --min-copies 1
    passes. (At 2 it does not: only-here.txt sits on a single medium.)"""
    stocked("redundancy", "--min-copies", "1", "--exit-code")   # no raise


def test_only_on_exit_code_gates_wiping_a_drive(stocked):
    """`holdings only-on old-drive --exit-code || wipe` — the workflow the
    README describes, made actually gateable."""
    with pytest.raises(SystemExit) as e:
        stocked("only-on", "a", "--exit-code")
    assert e.value.code == 1
    stocked("only-on", "b", "--exit-code")              # nothing only on b


def test_exit_code_and_json_compose(stocked, capsys):
    with pytest.raises(SystemExit):
        stocked("redundancy", "--min-copies", "3", "--exit-code", "--json")
    assert json.loads(capsys.readouterr().out)["below"] > 0


def test_check_json_reports_drift(stocked, db, capsys):
    stocked("check", "--json")
    assert json.loads(capsys.readouterr().out) == {"consistent": True,
                                                   "problems": []}
    with sqlite3.connect(db) as c:
        c.execute("UPDATE content SET backup_copies=9")
    with pytest.raises(SystemExit):
        stocked("check", "--json")
    doc = json.loads(capsys.readouterr().out)
    assert doc["consistent"] is False and doc["problems"]


# ----------------------------------------------------- durability classes

# The hazard these close: `--exit-code` turned redundancy into a gate that
# scripts trust, while `--backup` was an unverified claim covering media
# that fail in quite different ways.

def test_a_sync_mirror_is_not_a_backup_copy(run, db, capsys, tmp_path):
    """The one that was actively misleading. A Dropbox or Syncthing folder
    is scannable as an ordinary path, so nothing stopped it being marked
    --backup — after which redundancy reported two copies of a file that a
    single rm removes from both."""
    laptop, mirror = tmp_path / "laptop", tmp_path / "mirror"
    write(laptop / "photo.jpg", "photo")
    write(mirror / "photo.jpg", "photo")
    run("add-medium", "laptop", "--kind", "laptop")
    run("add-medium", "dropbox", "--kind", "cloud", "--durability", "mirror")
    run("scan", "laptop", str(laptop))
    run("scan", "dropbox", str(mirror))
    capsys.readouterr()

    with sqlite3.connect(db) as c:
        assert c.execute("SELECT is_backup FROM media"
                         " WHERE medium_id='dropbox'").fetchone()[0] == 0
        assert c.execute("SELECT copies, backup_copies FROM content"
                         ).fetchone() == (2, 0)
    with pytest.raises(SystemExit):
        run("redundancy", "--min-copies", "1", "--exit-code")


def test_an_independent_copy_is_a_backup_copy(run, db, capsys, tmp_path):
    laptop, drive = tmp_path / "laptop", tmp_path / "drive"
    write(laptop / "photo.jpg", "photo")
    write(drive / "photo.jpg", "photo")
    run("add-medium", "laptop", "--kind", "laptop")
    run("add-medium", "drive", "--kind", "drive", "--durability", "independent")
    run("scan", "laptop", str(laptop))
    run("scan", "drive", str(drive))
    capsys.readouterr()
    run("redundancy", "--min-copies", "1", "--exit-code")     # no raise


def test_backup_flag_still_means_independent(run, db):
    """Existing catalogs and existing muscle memory keep working."""
    run("add-medium", "d", "--kind", "drive", "--backup")
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT durability, is_backup FROM media"
                         ).fetchone() == ("independent", 1)


def test_is_backup_is_derived_not_asserted(run, db, capsys, tmp_path):
    """Hand-setting the column is drift, not configuration."""
    run("add-medium", "d", "--kind", "cloud", "--durability", "mirror")
    with sqlite3.connect(db) as c:
        c.execute("UPDATE media SET is_backup=1")          # the old way
    with pytest.raises(SystemExit):
        run("check")
    assert "disagrees with durability" in capsys.readouterr().err


# ------------------------------------------------------------- leases

def test_a_lease_needs_an_end_date(run):
    with pytest.raises(SystemExit) as e:
        run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased")
    assert "forever" in str(e.value)


def test_lease_expiry_accepts_a_duration_or_a_date(run, db):
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "30d")
    with sqlite3.connect(db) as c:
        expires, checked = c.execute(
            "SELECT lease_expires, lease_checked FROM media").fetchone()
    assert 29 < (expires - time.time()) / 86400 < 31
    assert abs(checked - time.time()) < 60        # the estimate is dated


def test_a_healthy_lease_counts_as_a_backup(run, db, capsys, tmp_path):
    write(tmp_path / "d" / "a.txt", "a")
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "300d")
    run("scan", "swarm", str(tmp_path / "d"))
    capsys.readouterr()
    run("redundancy", "--min-copies", "1", "--exit-code")     # no raise


def test_a_lapsed_lease_is_a_policy_violation(run, db, capsys, tmp_path):
    """A backup you stopped paying for is not a backup — and it lapses with
    no write happening, so it cannot be caught by the materialised counts."""
    write(tmp_path / "d" / "a.txt", "a")
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "300d")
    run("scan", "swarm", str(tmp_path / "d"))
    with sqlite3.connect(db) as c:                # time passes, nothing writes
        c.execute("UPDATE media SET lease_expires=?", (time.time() - 86400,))
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        run("redundancy", "--min-copies", "1", "--exit-code")
    assert e.value.code == 1
    assert "lapsed" in capsys.readouterr().err


def test_a_lease_inside_the_margin_is_at_risk(run, db, capsys, tmp_path):
    write(tmp_path / "d" / "a.txt", "a")
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "5d")
    run("scan", "swarm", str(tmp_path / "d"))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run("redundancy", "--min-copies", "1", "--lease-margin", "14",
            "--exit-code")
    assert "5 days left" in capsys.readouterr().err
    run("redundancy", "--min-copies", "1", "--lease-margin", "1",
        "--exit-code")                                        # no raise


def test_an_old_lease_estimate_is_not_trusted_silently(run, db, capsys,
                                                       tmp_path):
    """A node's TTL is an estimate at the price of the day; if the price
    rises the batch drains faster than quoted."""
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "300d")
    with sqlite3.connect(db) as c:
        c.execute("UPDATE media SET lease_checked=?",
                  (time.time() - 200 * 86400,))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run("redundancy", "--exit-code")
    assert "estimate is 200 days old" in capsys.readouterr().err


def test_re_registering_a_medium_keeps_its_lease(run, db, capsys):
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "300d")
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--location", "moved")
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT lease_expires FROM media"
                         ).fetchone()[0] is not None


def test_redundancy_json_reports_leases_at_risk(run, db, capsys, tmp_path):
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "1d")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run("redundancy", "--json", "--exit-code")
    doc = json.loads(capsys.readouterr().out)
    assert doc["leases_at_risk"][0]["medium_id"] == "swarm"


def test_an_old_catalog_keeps_what_it_claimed(tmp_path, capsys):
    """Migration must not silently downgrade an existing backup: is_backup=1
    was a claim that the copy survives deletion, which is `independent`."""
    db = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(db)
    old.executescript(OLD_INSTANCES)
    old.execute("INSERT INTO media (medium_id, kind, is_backup)"
                " VALUES ('drive-budapest','drive',1)")
    old.execute("INSERT INTO media (medium_id, kind, is_backup)"
                " VALUES ('laptop','laptop',0)")
    old.commit()
    old.close()

    conn = holdings.db_connect(db)
    try:
        assert dict(conn.execute(
            "SELECT medium_id, durability FROM media")) == {
                "drive-budapest": "independent", "laptop": "working"}
        assert holdings.derived_drift(conn) == []
    finally:
        conn.close()


# ------------------------------------------------- unreadable, not deleted

def test_an_unreadable_file_is_not_pruned_as_deleted(run, db, capsys,
                                                     tmp_path):
    """A failing drive and a tidied-up one look identical to the prune.

    Only one of them means the copy is gone, and getting it wrong lowers
    the recorded copy count on content that is still there — the exact
    direction this tool must never err in.
    """
    root = tmp_path / "drive"
    write(root / "readable.txt", "fine")
    bad = write(root / "rotten.txt", "was fine")
    run("add-medium", "d", "--kind", "drive", "--durability", "independent")
    run("scan", "d", str(root))
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM instances").fetchone()[0] == 2

    bad.chmod(0o000)                       # now unreadable, still present
    try:
        run("scan", "d", str(root), "--full")
        err = capsys.readouterr().err
        with sqlite3.connect(db) as c:
            paths = {p for p, in c.execute("SELECT path FROM instances")}
        assert paths == {"readable.txt", "rotten.txt"}, "the entry was pruned"
        assert "could not be read" in err
    finally:
        bad.chmod(0o644)


def test_a_genuinely_removed_file_is_still_pruned(run, db, capsys, tmp_path):
    """The fix must not make the catalog stop noticing real deletions."""
    root = tmp_path / "drive"
    write(root / "keep.txt", "keep")
    gone = write(root / "gone.txt", "gone")
    run("add-medium", "d", "--kind", "drive")
    run("scan", "d", str(root))
    gone.unlink()
    run("scan", "d", str(root))
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        assert {p for p, in c.execute("SELECT path FROM instances")} \
            == {"keep.txt"}


# --------------------------------------------------------- sites and 3-2-1

@pytest.fixture
def two_drives_one_room(run, tmp_path, capsys):
    """The case that motivated sites: two backup drives, same room."""
    src, d1, d2 = tmp_path / "src", tmp_path / "d1", tmp_path / "d2"
    write(src / "photo.jpg", "photo")
    write(d1 / "photo.jpg", "photo")
    write(d2 / "photo.jpg", "photo")
    run("add-medium", "laptop", "--kind", "laptop", "--site", "home")
    run("add-medium", "drive-a", "--kind", "drive",
        "--durability", "independent", "--site", "home")
    run("add-medium", "drive-b", "--kind", "drive",
        "--durability", "independent", "--site", "home")
    run("scan", "laptop", str(src))
    run("scan", "drive-a", str(d1))
    run("scan", "drive-b", str(d2))
    capsys.readouterr()
    return run


def test_two_copies_in_one_room_satisfy_copies_but_not_sites(
        two_drives_one_room, db, capsys):
    """Fire, flood, theft and a ransomware process walking mounted volumes
    take both. The count says two; the separation says one."""
    run = two_drives_one_room
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT backup_copies, backup_sites, backup_kinds"
                         " FROM content").fetchone() == (2, 1, 1)
    run("redundancy", "--min-copies", "2", "--exit-code")     # passes
    with pytest.raises(SystemExit) as e:
        run("redundancy", "--min-copies", "2", "--min-sites", "2",
            "--exit-code")
    assert e.value.code == 1


def test_moving_one_drive_offsite_satisfies_the_policy(
        two_drives_one_room, capsys):
    run = two_drives_one_room
    run("add-medium", "drive-b", "--kind", "drive",
        "--durability", "independent", "--site", "budapest")
    capsys.readouterr()
    run("redundancy", "--min-copies", "2", "--min-sites", "2", "--exit-code")


def test_media_without_a_site_are_not_assumed_separate(run, capsys, tmp_path):
    """Conservative by design: unknown is not the same as known-different,
    so unsited media collapse into one site rather than inflating the count."""
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    write(d1 / "a.txt", "a")
    write(d2 / "a.txt", "a")
    run("add-medium", "x", "--kind", "drive", "--durability", "independent")
    run("add-medium", "y", "--kind", "drive", "--durability", "independent")
    run("scan", "x", str(d1))
    run("scan", "y", str(d2))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run("redundancy", "--min-copies", "2", "--min-sites", "2",
            "--exit-code")


def test_min_kinds_is_the_two_media_types_of_321(run, db, capsys, tmp_path):
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    write(d1 / "a.txt", "a")
    write(d2 / "a.txt", "a")
    run("add-medium", "drive", "--kind", "drive",
        "--durability", "independent", "--site", "home")
    run("add-medium", "restic", "--kind", "restic-repo",
        "--durability", "independent", "--site", "cloud")
    run("scan", "drive", str(d1))
    run("scan", "restic", str(d2))
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT backup_kinds FROM content").fetchone()[0] == 2
    run("redundancy", "--min-copies", "2", "--min-sites", "2",
        "--min-kinds", "2", "--exit-code")


def test_a_site_survives_re_registering_the_medium(run, db, capsys):
    run("add-medium", "d", "--kind", "drive", "--site", "budapest")
    run("add-medium", "d", "--kind", "drive", "--location", "moved shelf")
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT site FROM media").fetchone()[0] == "budapest"


def test_the_full_policy_report_names_what_failed(two_drives_one_room,
                                                  capsys):
    run = two_drives_one_room
    with pytest.raises(SystemExit):
        run("redundancy", "--min-copies", "2", "--min-sites", "2",
            "--exit-code")
    out = capsys.readouterr().out
    assert "2 sites" in out and "SITE" in out


def test_policy_json_carries_the_thresholds(two_drives_one_room, capsys):
    run = two_drives_one_room
    with pytest.raises(SystemExit):
        run("redundancy", "--min-sites", "2", "--json", "--exit-code")
    doc = json.loads(capsys.readouterr().out)
    assert doc["min_sites"] == 2
    assert doc["items"][0]["backup_sites"] == 1


# ------------------------------------------- seen, versus actually verified

def test_a_rescan_does_not_re_read_the_file(run, db, capsys, tmp_path):
    """The distinction the columns exist for: `seen_at` means the filesystem
    still listed it at this size. Only `verified_at` means someone read the
    bytes — and bit rot changes neither size nor mtime."""
    root = tmp_path / "drive"
    write(root / "a.txt", "content")
    run("add-medium", "d", "--kind", "drive", "--durability", "independent")
    run("scan", "d", str(root))
    with sqlite3.connect(db) as c:
        first, evidence = c.execute(
            "SELECT verified_at, evidence FROM instances").fetchone()
    assert evidence == "hashed" and first is not None

    run("scan", "d", str(root))                    # unchanged: no read
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        seen, verified, evidence = c.execute(
            "SELECT seen_at, verified_at, evidence FROM instances").fetchone()
    assert evidence == "metadata"
    assert verified == first, "verification date must not advance on a re-list"
    assert seen > verified, "but the sighting is fresh"


def test_a_full_scan_re_verifies(run, db, capsys, tmp_path):
    root = tmp_path / "drive"
    write(root / "a.txt", "content")
    run("add-medium", "d", "--kind", "drive", "--durability", "independent")
    run("scan", "d", str(root))
    with sqlite3.connect(db) as c:
        first = c.execute("SELECT verified_at FROM instances").fetchone()[0]
    run("scan", "d", str(root), "--full")
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        again, evidence = c.execute(
            "SELECT verified_at, evidence FROM instances").fetchone()
    assert again > first and evidence == "hashed"


def test_content_changing_under_us_is_reported_not_swallowed(
        run, db, capsys, tmp_path):
    """On a medium nobody edits, a changed hash is what rot looks like. The
    upsert used to replace the hash and say nothing."""
    root = tmp_path / "drive"
    f = write(root / "a.txt", "original")
    run("add-medium", "d", "--kind", "drive", "--durability", "independent")
    run("scan", "d", str(root))
    capsys.readouterr()

    f.write_bytes(b"rotted!!")                     # same length, new bytes
    run("scan", "d", str(root), "--full")
    err = capsys.readouterr().err
    assert "hashed to something other than the catalog recorded" in err
    assert "was sha256:" in err and "now sha256:" in err


def test_imported_listings_are_marked_as_the_weakest_evidence(
        run, db, capsys, tmp_path):
    listing = tmp_path / "ls.json"
    listing.write_text(json.dumps(
        {"struct_type": "node", "type": "file", "path": "/x.pdf", "size": 9}))
    run("add-medium", "r", "--kind", "restic-repo",
        "--durability", "independent")
    run("import-restic", "r", str(listing))
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT evidence, verified_at FROM instances"
                         ).fetchone() == ("imported", None)


def test_verified_within_discounts_stale_evidence(run, db, capsys, tmp_path):
    """A copy nobody has read in years is weak evidence — and for the drive
    in the safe in another country, that is the normal state."""
    root = tmp_path / "drive"
    write(root / "a.txt", "content")
    run("add-medium", "d", "--kind", "drive", "--durability", "independent")
    run("scan", "d", str(root))
    capsys.readouterr()
    run("redundancy", "--min-copies", "1", "--verified-within", "30",
        "--exit-code")                             # just read it: passes

    with sqlite3.connect(db) as c:                 # two years pass
        old = time.time() - 730 * 86400
        c.execute("UPDATE instances SET verified_at=?", (old,))
        c.execute("UPDATE content SET backup_verified_at=?", (old,))
    with pytest.raises(SystemExit):
        run("redundancy", "--min-copies", "1", "--verified-within", "30",
            "--exit-code")
    assert "read within 30d" in capsys.readouterr().out


def test_never_verified_content_counts_as_unverified(run, db, capsys,
                                                     tmp_path):
    listing = tmp_path / "ls.json"
    listing.write_text(json.dumps(
        {"struct_type": "node", "type": "file", "path": "/x.pdf", "size": 9}))
    run("add-medium", "r", "--kind", "restic-repo",
        "--durability", "independent")
    run("import-restic", "r", str(listing))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run("redundancy", "--min-copies", "1", "--verified-within", "365",
            "--exit-code")
    assert "never" in capsys.readouterr().out


def test_media_reports_when_it_was_last_actually_read(run, db, capsys,
                                                      tmp_path):
    root = tmp_path / "drive"
    write(root / "a.txt", "content")
    run("add-medium", "d", "--kind", "drive", "--durability", "independent")
    run("scan", "d", str(root))
    capsys.readouterr()
    run("media")
    out = capsys.readouterr().out
    assert "LAST READ" in out
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT verified_at FROM media").fetchone()[0]


# ----------------------------------------------------- verification schedule

@pytest.fixture
def two_backups(run, tmp_path, capsys):
    src, bp, home = tmp_path / "src", tmp_path / "bp", tmp_path / "home"
    write(src / "shared.jpg", "shared")
    write(bp / "shared.jpg", "shared")
    write(bp / "only-backed-here.txt", "sole")
    write(home / "other.txt", "other")
    run("add-medium", "laptop", "--kind", "laptop", "--site", "home")
    run("add-medium", "drive-bp", "--kind", "drive",
        "--durability", "independent", "--site", "budapest")
    run("add-medium", "drive-home", "--kind", "drive",
        "--durability", "independent", "--site", "home")
    run("scan", "laptop", str(src))
    run("scan", "drive-bp", str(bp))
    run("scan", "drive-home", str(home))
    capsys.readouterr()
    return run


def age_medium(db, medium_id, days):
    with sqlite3.connect(db) as c:
        when = time.time() - days * 86400
        c.execute("UPDATE instances SET verified_at=? WHERE medium_id=?",
                  (when, medium_id))
        c.execute("UPDATE media SET verified_at=? WHERE medium_id=?",
                  (when, medium_id))


def test_sole_backup_counts_what_re_reading_would_protect(two_backups, db):
    """Distinct from only_here: a working copy may exist elsewhere, but
    nothing else would survive deleting it."""
    with sqlite3.connect(db) as c:
        rows = dict(c.execute(
            "SELECT medium_id, sole_backup_count FROM media"))
    # shared.jpg: on laptop (working) + drive-bp -> drive-bp is its only
    # backup. only-backed-here.txt: only on drive-bp. other.txt: only on
    # drive-home.
    assert rows == {"laptop": 0, "drive-bp": 2, "drive-home": 1}


def test_a_working_medium_is_not_on_the_schedule(two_backups, capsys):
    """You do not verify the copy you edit; it is the original."""
    two_backups("due")
    assert "laptop" not in capsys.readouterr().out


def test_nothing_is_overdue_right_after_a_scan(two_backups, capsys):
    two_backups("due", "--exit-code")            # no raise
    assert "!" not in capsys.readouterr().out


def test_an_unread_medium_becomes_overdue(two_backups, db, capsys):
    age_medium(db, "drive-bp", 300)
    with pytest.raises(SystemExit) as e:
        two_backups("due", "--exit-code")
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "!drive-bp" in out and "300d" in out


def test_the_staleness_threshold_is_the_users_to_set(two_backups, db,
                                                     capsys):
    age_medium(db, "drive-bp", 200)
    with pytest.raises(SystemExit):
        two_backups("due", "--stale-after", "180", "--exit-code")
    capsys.readouterr()
    two_backups("due", "--stale-after", "365", "--exit-code")   # no raise


def test_media_holding_the_only_backup_outrank_older_ones(
        two_backups, db, tmp_path, capsys):
    """The primary key: what would hurt most to lose, before what has gone
    longest unread. A medium whose content is backed up elsewhere too is
    less urgent than one holding the only copy, however long ago it was
    read."""
    run = two_backups
    # Give drive-home's content a second backup, so nothing is sole to it.
    second = tmp_path / "second"
    write(second / "other.txt", "other")
    run("add-medium", "drive-spare", "--kind", "drive",
        "--durability", "independent", "--site", "home")
    run("scan", "drive-spare", str(second))
    age_medium(db, "drive-home", 900)            # much older, nothing at stake
    age_medium(db, "drive-bp", 10)               # recent, holds sole backups
    capsys.readouterr()

    run("due")
    lines = [l for l in capsys.readouterr().out.splitlines() if "drive-" in l]
    assert "drive-bp" in lines[0], lines


def test_among_equals_the_longest_unread_comes_first(two_backups, db,
                                                     capsys):
    """The tiebreak, once the stakes are the same."""
    age_medium(db, "drive-bp", 100)
    age_medium(db, "drive-home", 400)
    two_backups("due")
    lines = [l for l in capsys.readouterr().out.splitlines() if "drive-" in l]
    assert "drive-home" in lines[0] and "drive-bp" in lines[1], lines


def test_due_json_gives_the_plan_as_data(two_backups, db, capsys):
    age_medium(db, "drive-bp", 300)
    with pytest.raises(SystemExit):
        two_backups("due", "--json", "--exit-code")
    doc = json.loads(capsys.readouterr().out)
    assert doc["overdue"] == 1
    bp = next(m for m in doc["media"] if m["medium_id"] == "drive-bp")
    assert bp["overdue"] is True
    assert bp["sole_backup_for"] == 2
    assert 299 < bp["days_since_verified"] < 301
    assert bp["site"] == "budapest"


def test_a_never_verified_medium_is_overdue(run, db, capsys, tmp_path):
    """An imported listing is never read, so it never verifies anything."""
    listing = tmp_path / "ls.json"
    listing.write_text(json.dumps(
        {"struct_type": "node", "type": "file", "path": "/x.pdf", "size": 9}))
    run("add-medium", "r", "--kind", "restic-repo",
        "--durability", "independent")
    run("import-restic", "r", str(listing))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run("due", "--exit-code")
    assert "never" in capsys.readouterr().out


def test_due_is_read_only_so_it_works_on_a_published_catalog():
    import argparse
    [subs] = [a for a in holdings.build_parser()._actions
              if isinstance(a, argparse._SubParsersAction)]
    assert subs.choices["due"].get_default("writes") is False


# ------------------------------------------------------- Swarm as a medium

def swarm_listing_file(tmp_path, entries):
    f = tmp_path / "swarm.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return str(f)


@pytest.fixture
def swarm_medium(run, capsys):
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "300d", "--site", "swarm")
    capsys.readouterr()
    return run


def test_import_swarm_records_the_reference_alongside_the_hash(
        swarm_medium, db, tmp_path, capsys):
    """Content hash stays the identity; the Swarm reference is an address on
    one medium, and storing it is what makes the copy checkable later
    without downloading it again."""
    listing = swarm_listing_file(tmp_path, [
        {"path": "photos/holiday.jpg", "size": 6, "reference": "abc123"},
    ])
    swarm_medium("import-swarm", "swarm", listing)
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        path, ref, evidence, verified = c.execute(
            "SELECT path, external_ref, evidence, verified_at FROM instances"
        ).fetchone()
    assert path == "photos/holiday.jpg"
    assert ref == "abc123"
    assert evidence == "imported" and verified is None


def test_import_swarm_matches_content_already_known_by_hash(
        run, db, tmp_path, capsys):
    """Unambiguous name+size gets the real hash, so the copy counts."""
    src = tmp_path / "src"
    write(src / "holiday.jpg", "photo!")          # 6 bytes
    run("add-medium", "laptop", "--kind", "laptop")
    run("scan", "laptop", str(src))
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "300d")
    listing = swarm_listing_file(tmp_path, [
        {"path": "holiday.jpg", "size": 6, "reference": "deadbeef"}])
    run("import-swarm", "swarm", listing)
    out = capsys.readouterr().out
    assert "1 matched to content already known by hash" in out
    with sqlite3.connect(db) as c:
        hashes = {h for h, in c.execute("SELECT hash FROM instances")}
    assert len(hashes) == 1 and hashes.pop().startswith("sha256:")


def test_unmatched_entries_are_marked_unverified_not_invented(
        swarm_medium, db, tmp_path, capsys):
    """A listing proves paths and sizes, never bytes."""
    listing = swarm_listing_file(tmp_path, [
        {"path": "mystery.bin", "size": 999, "reference": "ref"}])
    swarm_medium("import-swarm", "swarm", listing)
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        h = c.execute("SELECT hash FROM instances").fetchone()[0]
    assert h.startswith("unverified:swarm:")


def test_a_swarm_medium_counts_as_a_leased_backup(run, db, tmp_path, capsys):
    src = tmp_path / "src"
    write(src / "a.txt", "aaa")
    run("add-medium", "laptop", "--kind", "laptop")
    run("scan", "laptop", str(src))
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "300d")
    run("import-swarm", "swarm",
        swarm_listing_file(tmp_path, [
            {"path": "a.txt", "size": 3, "reference": "r"}]))
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT backup_copies FROM content"
                         ).fetchone()[0] == 1
    run("redundancy", "--min-copies", "1", "--exit-code")     # no raise


def test_a_lapsed_swarm_lease_stops_counting(run, db, tmp_path, capsys):
    """The whole reason Swarm needed the lease machinery first."""
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "300d")
    run("import-swarm", "swarm",
        swarm_listing_file(tmp_path, [
            {"path": "a.txt", "size": 3, "reference": "r"}]))
    with sqlite3.connect(db) as c:
        c.execute("UPDATE media SET lease_expires=?", (time.time() - 86400,))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run("redundancy", "--min-copies", "1", "--exit-code")
    assert "lapsed" in capsys.readouterr().err


def test_re_importing_prunes_what_is_no_longer_there(swarm_medium, db,
                                                     tmp_path, capsys):
    swarm_medium("import-swarm", "swarm", swarm_listing_file(tmp_path, [
        {"path": "a.txt", "size": 1, "reference": "r1"},
        {"path": "b.txt", "size": 2, "reference": "r2"}]))
    swarm_medium("import-swarm", "swarm", swarm_listing_file(tmp_path, [
        {"path": "a.txt", "size": 1, "reference": "r1"}]))
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        assert {p for p, in c.execute("SELECT path FROM instances")} \
            == {"a.txt"}


def test_import_swarm_refuses_an_unregistered_medium(run, tmp_path):
    with pytest.raises(SystemExit) as e:
        run("import-swarm", "nope",
            swarm_listing_file(tmp_path, [{"path": "a", "size": 1}]))
    assert "register it first" in str(e.value)


def test_listing_a_root_without_the_extra_says_how_to_install(
        swarm_medium, monkeypatch):
    no_swarmlite_import(monkeypatch, "fsspec")
    with pytest.raises(SystemExit) as e:
        swarm_medium("import-swarm", "swarm", "--root", "deadbeef")
    assert "holdings[swarm]" in str(e.value)


def test_lease_from_batch_without_the_extra_says_how_to_install(
        run, monkeypatch):
    no_swarmlite_import(monkeypatch, "swarmlite")
    with pytest.raises(SystemExit) as e:
        run("add-medium", "s", "--kind", "cloud", "--durability", "leased",
            "--lease-from-batch", "abc123")
    assert "holdings[swarm]" in str(e.value)


def no_swarmlite_import(monkeypatch, name):
    """Simulate one optional dependency being absent, whether or not it is."""
    import builtins
    real = builtins.__import__

    def fake(mod, *a, **k):
        if mod == name:
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return real(mod, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)


# ------------------------------- checking a remote copy without fetching it

@pytest.fixture
def swarm_with_refs(run, tmp_path, capsys):
    run("add-medium", "swarm", "--kind", "cloud", "--durability", "leased",
        "--lease-expires", "300d")
    listing = tmp_path / "l.jsonl"
    listing.write_text("\n".join(json.dumps(e) for e in [
        {"path": "a.txt", "size": 1, "reference": "ref-a"},
        {"path": "b.txt", "size": 2, "reference": "ref-b"},
        {"path": "c.txt", "size": 3, "reference": "ref-c"},
    ]) + "\n")
    run("import-swarm", "swarm", str(listing))
    capsys.readouterr()
    return run


def fake_probe(monkeypatch, missing=()):
    """Stand in for the network, so the suite stays offline."""
    seen = []

    def probe(api_url, ref, deep, timeout):
        seen.append((ref, deep))
        return ref not in missing

    monkeypatch.setattr(holdings, "swarm_probe", probe)
    return seen


def test_all_copies_retrievable_is_a_clean_pass(swarm_with_refs, monkeypatch,
                                                capsys):
    fake_probe(monkeypatch)
    swarm_with_refs("check-swarm", "swarm", "--exit-code")     # no raise
    assert "3 retrievable, 0 not" in capsys.readouterr().out


def test_a_missing_copy_is_reported_and_fails_the_policy(
        swarm_with_refs, monkeypatch, capsys):
    fake_probe(monkeypatch, missing={"ref-b"})
    with pytest.raises(SystemExit) as e:
        swarm_with_refs("check-swarm", "swarm", "--exit-code")
    assert e.value.code == 1
    assert "MISSING  b.txt" in capsys.readouterr().err


def test_a_missing_copy_is_never_deleted_from_the_catalog(
        swarm_with_refs, db, monkeypatch, capsys):
    """Same lesson as an unreadable file on a failing drive: a node that
    searched and found nothing is good evidence, not proof, and quietly
    lowering the copy count is the direction this tool must not err in."""
    fake_probe(monkeypatch, missing={"ref-b"})
    with pytest.raises(SystemExit):
        swarm_with_refs("check-swarm", "swarm", "--exit-code")
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        assert {p for p, in c.execute("SELECT path FROM instances")} \
            == {"a.txt", "b.txt", "c.txt"}


def test_retrievability_updates_sighting_but_never_verification(
        swarm_with_refs, db, monkeypatch, capsys):
    """The network confirms a copy is reachable at that address. It does not
    confirm the bytes hash to what the catalog recorded — only reading them
    does that."""
    fake_probe(monkeypatch)
    swarm_with_refs("check-swarm", "swarm")
    capsys.readouterr()
    with sqlite3.connect(db) as c:
        for evidence, verified in c.execute(
                "SELECT evidence, verified_at FROM instances"):
            assert evidence == "retrievable"
            assert verified is None, "a probe must not claim verification"


def test_deep_is_opt_in(swarm_with_refs, monkeypatch, capsys):
    """The default probe is constant-time; stewardship walks every chunk and
    timed out entirely on a 138 MB file in live measurement."""
    seen = fake_probe(monkeypatch)
    swarm_with_refs("check-swarm", "swarm")
    assert all(deep is False for _, deep in seen)
    capsys.readouterr()
    seen.clear()
    swarm_with_refs("check-swarm", "swarm", "--deep")
    assert all(deep is True for _, deep in seen)


def test_check_swarm_json_lists_what_is_missing(swarm_with_refs, monkeypatch,
                                                capsys):
    fake_probe(monkeypatch, missing={"ref-a", "ref-c"})
    with pytest.raises(SystemExit):
        swarm_with_refs("check-swarm", "swarm", "--json", "--exit-code")
    doc = json.loads(capsys.readouterr().out)
    assert doc["retrievable"] == 1
    assert sorted(doc["missing"]) == ["a.txt", "c.txt"]


def test_a_medium_without_references_says_so(run, tmp_path, capsys):
    run("add-medium", "d", "--kind", "drive", "--durability", "independent")
    write(tmp_path / "disk" / "a.txt", "a")
    run("scan", "d", str(tmp_path / "disk"))
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        run("check-swarm", "d")
    assert "no instances carrying a reference" in str(e.value)


def test_the_probe_needs_no_optional_extra(monkeypatch):
    """urllib, not swarmlite: a reachable node is the only requirement."""
    no_swarmlite_import(monkeypatch, "swarmlite")
    no_swarmlite_import(monkeypatch, "fsspec")
    assert holdings.swarm_probe("http://127.0.0.1:1", "ref", False, 0.2) \
        is False        # unreachable, but it got as far as trying


# ------------------------------------------- "can I reformat this laptop?"

@pytest.fixture
def before_a_reformat(run, tmp_path, capsys):
    """A laptop, a real backup drive, and a sync mirror — the arrangement
    that makes the naive check wrong."""
    laptop, drive, mirror = (tmp_path / "laptop", tmp_path / "drive",
                             tmp_path / "mirror")
    write(laptop / "holiday.jpg", "photo")
    write(drive / "holiday.jpg", "photo")          # a real backup
    write(laptop / "thesis.pdf", "thesis!")
    write(mirror / "thesis.pdf", "thesis!")        # only in Dropbox
    write(laptop / "notes.txt", "scratch!")        # nowhere else
    run("add-medium", "laptop", "--kind", "laptop")
    run("add-medium", "drive", "--kind", "drive", "--durability",
        "independent")
    run("add-medium", "dropbox", "--kind", "cloud", "--durability", "mirror")
    run("scan", "laptop", str(laptop))
    run("scan", "drive", str(drive))
    run("scan", "dropbox", str(mirror))
    capsys.readouterr()
    return run


def test_only_on_alone_would_clear_a_laptop_that_is_not_safe(
        before_a_reformat, capsys):
    """Pinning the trap. `only-on` asks "does anything else hold this?", and
    a sync mirror answers yes — while being exactly the copy that does not
    survive. thesis.pdf has no backup and `only-on` stays silent about it."""
    before_a_reformat("only-on", "laptop")
    out = capsys.readouterr().out
    assert "notes.txt" in out
    assert "thesis.pdf" not in out, "only-on cannot answer the wipe question"


def test_scoped_redundancy_catches_what_only_on_misses(before_a_reformat,
                                                       capsys):
    before_a_reformat("redundancy", "--on", "laptop", "--min-copies", "1")
    out = capsys.readouterr().out
    assert "notes.txt" in out and "thesis.pdf" in out
    assert "holiday.jpg" not in out, "the genuinely backed-up file is quiet"


def test_the_reformat_gate(before_a_reformat, tmp_path, capsys):
    run = before_a_reformat
    with pytest.raises(SystemExit) as e:
        run("redundancy", "--on", "laptop", "--min-copies", "1",
            "--exit-code")
    assert e.value.code == 1

    drive = tmp_path / "drive"                    # back the stragglers up
    write(drive / "thesis.pdf", "thesis!")
    write(drive / "notes.txt", "scratch!")
    run("scan", "drive", str(drive))
    capsys.readouterr()
    run("redundancy", "--on", "laptop", "--min-copies", "1", "--exit-code")
    assert "everything on 'laptop'" in capsys.readouterr().out


def test_scoping_ignores_content_that_is_not_on_that_medium(
        before_a_reformat, run, tmp_path, capsys):
    """An unbacked file elsewhere must not block reformatting the laptop."""
    other = tmp_path / "other"
    write(other / "unrelated.bin", "unbacked")
    run("add-medium", "usb", "--kind", "drive")
    run("scan", "usb", str(other))
    capsys.readouterr()

    with pytest.raises(SystemExit):               # catalog-wide: still bad
        run("redundancy", "--min-copies", "1", "--exit-code")
    capsys.readouterr()
    with pytest.raises(SystemExit):               # laptop: bad for its own
        run("redundancy", "--on", "laptop", "--min-copies", "1",
            "--exit-code")
    out = capsys.readouterr().out
    assert "unrelated.bin" not in out


def test_scoping_does_not_double_count_a_file_held_twice(run, db, tmp_path,
                                                         capsys):
    """One content object at two paths on the same medium is one object."""
    laptop = tmp_path / "laptop"
    write(laptop / "a.txt", "same")
    write(laptop / "copy-of-a.txt", "same")
    run("add-medium", "laptop", "--kind", "laptop")
    run("scan", "laptop", str(laptop))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run("redundancy", "--on", "laptop", "--min-copies", "1", "--json",
            "--exit-code")
    doc = json.loads(capsys.readouterr().out)
    assert doc["below"] == 1 and doc["on"] == "laptop"


def test_scoping_to_an_unknown_medium_is_an_error(before_a_reformat):
    with pytest.raises(SystemExit) as e:
        before_a_reformat("redundancy", "--on", "nosuch")
    assert "unknown medium" in str(e.value)


def test_a_plain_scoped_report_does_not_claim_unset_thresholds(
        before_a_reformat, capsys):
    """--on must not imply site and kind constraints it is not applying."""
    before_a_reformat("redundancy", "--on", "laptop", "--min-copies", "1")
    out = capsys.readouterr().out
    assert "site" not in out and "SITE" not in out
