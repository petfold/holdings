"""Tests for `import-git` — GitHub, Hugging Face, Radicle.

All local: a bare repo and a clone in tmp_path, no network. What these pin
is the one thing that makes a hub different from a directory — a working
tree holds files the remote does not, and recording those as backed up
would be wrong for precisely the files someone is working on.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import holdings  # noqa: E402


def run_git(repo, *args):
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True)
    assert out.returncode == 0, f"git {args}: {out.stderr}"
    return out.stdout


@pytest.fixture
def repo(tmp_path):
    """A clone with an upstream, so there is a real remote ref to read."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True,
                   capture_output=True)
    run_git(work, "config", "user.email", "t@example.invalid")
    run_git(work, "config", "user.name", "Test")
    return work


def commit_and_push(repo, files: dict):
    for name, content in files.items():
        f = repo / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "commit")
    branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    run_git(repo, "push", "-q", "origin", branch)
    run_git(repo, "branch", f"--set-upstream-to=origin/{branch}")
    return branch


@pytest.fixture
def cli(tmp_path):
    db = str(tmp_path / "c.sqlite")

    def _run(*argv):
        return holdings.main(["--db", db, *argv])

    _run.db = db
    return _run


@pytest.fixture
def hosted(cli, capsys):
    cli("add-medium", "github", "--kind", "other", "--durability", "hosted")
    capsys.readouterr()
    return cli


def paths_on(db, medium="github"):
    with sqlite3.connect(db) as c:
        return {p for p, in c.execute(
            "SELECT path FROM instances WHERE medium_id=?", (medium,))}


# ------------------------------------------------ what the remote actually has

def test_only_pushed_content_is_recorded(repo, hosted, capsys):
    """The distinction the whole command exists for."""
    commit_and_push(repo, {"pushed.txt": "on the remote"})
    (repo / "unpushed.txt").write_text("committed, not pushed")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "local only")

    hosted("import-git", "github", str(repo))
    capsys.readouterr()
    assert paths_on(hosted.db) == {"pushed.txt"}


def test_being_ahead_of_the_remote_is_reported(repo, hosted, capsys):
    commit_and_push(repo, {"a.txt": "a"})
    (repo / "b.txt").write_text("b")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "ahead")

    hosted("import-git", "github", str(repo))
    err = capsys.readouterr().err
    assert "1 commit(s) ahead" in err
    assert "NOT on the remote" in err


def test_untracked_and_ignored_files_are_not_on_the_hub(repo, hosted,
                                                        capsys):
    commit_and_push(repo, {"tracked.txt": "yes", ".gitignore": "secret.key\n"})
    (repo / "untracked.txt").write_text("no")
    (repo / "secret.key").write_text("definitely not")

    hosted("import-git", "github", str(repo))
    err = capsys.readouterr().err
    assert paths_on(hosted.db) == {"tracked.txt", ".gitignore"}
    assert "uncommitted or untracked" in err


def test_a_clean_pushed_repo_warns_about_nothing(repo, hosted, capsys):
    commit_and_push(repo, {"a.txt": "a"})
    hosted("import-git", "github", str(repo))
    assert capsys.readouterr().err == ""


# ------------------------------------------------------------ exact identity

def test_hashes_match_a_scan_of_the_same_bytes(repo, hosted, tmp_path,
                                               capsys):
    """The happy accident that makes this worth doing: the local object
    store has the bytes, so a hub's contents are exact without a network
    round trip."""
    commit_and_push(repo, {"same.txt": "identical bytes"})
    disk = tmp_path / "disk"
    disk.mkdir()
    (disk / "same.txt").write_text("identical bytes")

    hosted("add-medium", "laptop", "--kind", "laptop")
    hosted("scan", "laptop", str(disk))
    hosted("import-git", "github", str(repo))
    out = capsys.readouterr().out
    assert "1 by hash (exact)" in out

    with sqlite3.connect(hosted.db) as c:
        hashes = {h for h, in c.execute("SELECT DISTINCT hash FROM instances")}
    assert len(hashes) == 1, "one content object, seen on two media"


def test_a_pushed_file_counts_as_a_backup_of_the_working_copy(
        repo, hosted, tmp_path, capsys):
    commit_and_push(repo, {"work.txt": "shared"})
    hosted("add-medium", "laptop", "--kind", "laptop")
    hosted("scan", "laptop", str(repo / ".git" / ".."))
    hosted("import-git", "github", str(repo))
    capsys.readouterr()
    hosted("redundancy", "--on", "laptop", "--min-copies", "1")
    assert "work.txt" not in capsys.readouterr().out


# ------------------------------------------------------------- git-lfs

LFS_POINTER = ("version https://git-lfs.github.com/spec/v1\n"
               "oid sha256:" + "b" * 64 + "\n"
               "size 4096\n")


def test_an_lfs_pointer_records_the_content_it_points_at(repo, hosted,
                                                         capsys):
    """A Hugging Face model repo is pointers almost all the way down.
    Hashing them would record a few hundred bytes of text as the weights."""
    commit_and_push(repo, {"model.bin": LFS_POINTER})
    hosted("import-git", "github", str(repo))
    capsys.readouterr()
    with sqlite3.connect(hosted.db) as c:
        h, size = c.execute(
            "SELECT hash, size FROM instances WHERE path='model.bin'"
        ).fetchone()
    assert h == "sha256:" + "b" * 64
    assert size == 4096, "the pointer's own length must not be recorded"


def test_a_malformed_pointer_is_treated_as_an_ordinary_file(repo, hosted,
                                                            capsys):
    """Better to record the bytes that are actually there than to trust a
    header and invent a size."""
    broken = "version https://git-lfs.github.com/spec/v1\nnothing else\n"
    commit_and_push(repo, {"odd.bin": broken})
    hosted("import-git", "github", str(repo))
    capsys.readouterr()
    with sqlite3.connect(hosted.db) as c:
        size = c.execute("SELECT size FROM instances WHERE path='odd.bin'"
                         ).fetchone()[0]
    assert size == len(broken)


def test_parse_lfs_pointer_directly():
    assert holdings.parse_lfs_pointer(b"not a pointer") is None
    got = holdings.parse_lfs_pointer(LFS_POINTER.encode())
    assert got == {"hash": "b" * 64, "size": 4096}


# --------------------------------------------------------------- plumbing

def test_symlinks_are_skipped_as_they_are_by_scan(repo, hosted, capsys):
    (repo / "real.txt").write_text("real")
    (repo / "link.txt").symlink_to("real.txt")
    commit_and_push(repo, {})
    hosted("import-git", "github", str(repo))
    capsys.readouterr()
    assert paths_on(hosted.db) == {"real.txt"}


def test_an_explicit_ref_is_honoured(repo, hosted, capsys):
    branch = commit_and_push(repo, {"a.txt": "a"})
    hosted("import-git", "github", str(repo), "--ref", f"origin/{branch}")
    assert f"from origin/{branch}" in capsys.readouterr().out


def test_an_unresolvable_ref_explains_itself(tmp_path, hosted):
    """A repo with no upstream: say what to do, not just what failed."""
    plain = tmp_path / "plain"
    subprocess.run(["git", "init", "-q", str(plain)], check=True)
    run_git(plain, "config", "user.email", "t@example.invalid")
    run_git(plain, "config", "user.name", "Test")
    (plain / "a.txt").write_text("a")
    run_git(plain, "add", "-A")
    run_git(plain, "commit", "-qm", "only local")

    with pytest.raises(SystemExit) as e:
        hosted("import-git", "github", str(plain))
    assert "--ref" in str(e.value) and "git fetch" in str(e.value)


def test_import_git_refuses_an_unregistered_medium(repo, cli):
    with pytest.raises(SystemExit) as e:
        cli("import-git", "nope", str(repo))
    assert "register it first" in str(e.value)
