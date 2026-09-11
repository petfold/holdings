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
    """Two different files, same name: the tool must not pick one."""
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
