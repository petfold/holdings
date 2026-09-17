"""Tests for `import-syncthing`.

A stub of Syncthing's REST API on loopback — the response shapes here were
taken from a real instance, not invented. What they pin is the reason to use
the API at all: it knows what the cluster holds and how complete each device
is, which a directory walk cannot tell you.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import holdings  # noqa: E402

API_KEY = "test-key"

FOLDERS = [{"id": "docs", "label": "Documents", "path": "/srv/docs",
            "type": "sendreceive",
            "devices": [{"deviceID": "LOCAL-DEVICE"},
                        {"deviceID": "OTHER-DEVICE"}]}]

BROWSE = [
    {"name": "holiday.jpg", "size": 17, "type": "FILE_INFO_TYPE_FILE"},
    {"name": "sub", "size": 128, "type": "FILE_INFO_TYPE_DIRECTORY",
     "children": [
         {"name": "notes.txt", "size": 6, "type": "FILE_INFO_TYPE_FILE"},
         {"name": "deeper", "size": 64, "type": "FILE_INFO_TYPE_DIRECTORY",
          "children": [{"name": "x.bin", "size": 3,
                        "type": "FILE_INFO_TYPE_FILE"}]},
     ]},
]


@pytest.fixture
def syncthing():
    """A stub speaking the shapes a real instance answered with."""
    state = {"completion": 100, "auth": True}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if state["auth"] and self.headers.get("X-API-Key") != API_KEY:
                self.send_error(403)
                return
            if self.path.startswith("/rest/config/folders"):
                body = FOLDERS
            elif self.path.startswith("/rest/db/browse"):
                body = BROWSE
            elif self.path.startswith("/rest/system/status"):
                body = {"myID": "LOCAL-DEVICE"}
            elif self.path.startswith("/rest/db/completion"):
                body = {"completion": state["completion"], "globalItems": 3}
            else:
                self.send_error(404)
                return
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield state
    httpd.shutdown()


@pytest.fixture
def cli(tmp_path):
    db = str(tmp_path / "c.sqlite")

    def _run(*argv):
        return holdings.main(["--db", db, *argv])

    _run.db = db
    return _run


def import_args(state, *extra):
    return ("--api-url", state["url"], "--api-key", API_KEY, *extra)


# ------------------------------------------------------------- the tree

def test_flatten_browse_turns_the_tree_into_paths():
    got = list(holdings.flatten_browse(BROWSE))
    assert [e["path"] for e in got] == [
        "holiday.jpg", "sub/notes.txt", "sub/deeper/x.bin"]
    assert [e["size"] for e in got] == [17, 6, 3]


def test_directories_are_not_recorded_as_files():
    assert all("sub" != e["path"] for e in holdings.flatten_browse(BROWSE))


# ------------------------------------------------------------- discovery

def test_without_a_folder_it_lists_what_it_can_see(cli, syncthing, capsys):
    cli("import-syncthing", *import_args(syncthing))
    out = capsys.readouterr().out
    assert "docs" in out and "Documents" in out
    assert "a sync target is not a backup" in out


def test_an_unknown_folder_says_how_to_find_the_right_one(cli, syncthing,
                                                          capsys):
    cli("add-medium", "nas", "--kind", "other", "--durability", "mirror")
    with pytest.raises(SystemExit) as e:
        cli("import-syncthing", "nas", *import_args(syncthing,
                                                    "--folder", "nope"))
    assert "run without --folder" in str(e.value)


# --------------------------------------------------------------- import

def test_the_cluster_index_becomes_placement(cli, syncthing, capsys):
    cli("add-medium", "nas", "--kind", "other", "--durability", "mirror")
    cli("import-syncthing", "nas", *import_args(syncthing, "--folder",
                                                "docs"))
    capsys.readouterr()
    with sqlite3.connect(cli.db) as c:
        assert {p for p, in c.execute("SELECT path FROM instances")} == {
            "holiday.jpg", "sub/notes.txt", "sub/deeper/x.bin"}


def test_a_syncthing_peer_is_not_a_backup(cli, syncthing, tmp_path, capsys):
    """The point of the durability class. Two copies, no backup: a deletion
    reaches the peer, so wiping the laptop is not safe."""
    disk = tmp_path / "disk"
    (disk / "sub").mkdir(parents=True)
    (disk / "holiday.jpg").write_bytes(b"x" * 17)
    (disk / "sub" / "notes.txt").write_bytes(b"y" * 6)
    cli("add-medium", "laptop", "--kind", "laptop")
    cli("scan", "laptop", str(disk))
    cli("add-medium", "nas", "--kind", "other", "--durability", "mirror")
    cli("import-syncthing", "nas", *import_args(syncthing, "--folder",
                                                "docs"))
    capsys.readouterr()

    with sqlite3.connect(cli.db) as c:
        copies, backups = c.execute(
            "SELECT copies, backup_copies FROM content"
            " WHERE example_path='holiday.jpg'").fetchone()
    assert (copies, backups) == (2, 0)
    with pytest.raises(SystemExit):
        cli("redundancy", "--on", "laptop", "--min-copies", "1",
            "--exit-code")


def test_matching_uses_hashes_the_local_scan_already_knows(cli, syncthing,
                                                           tmp_path, capsys):
    disk = tmp_path / "disk"
    disk.mkdir()
    (disk / "holiday.jpg").write_bytes(b"x" * 17)
    cli("add-medium", "laptop", "--kind", "laptop")
    cli("scan", "laptop", str(disk))
    cli("add-medium", "nas", "--kind", "other", "--durability", "mirror")
    cli("import-syncthing", "nas", *import_args(syncthing, "--folder",
                                                "docs"))
    assert "1 matched by name+size" in capsys.readouterr().out


def test_an_incomplete_device_is_not_claimed_to_hold_everything(
        cli, syncthing, capsys):
    """db/browse is the cluster's index, not one device's disk. A device
    below 100% has not got all of it."""
    syncthing["completion"] = 42.5
    cli("add-medium", "nas", "--kind", "other", "--durability", "mirror")
    cli("import-syncthing", "nas", *import_args(syncthing, "--folder",
                                                "docs"))
    err = capsys.readouterr().err
    assert "42.5% complete" in err
    assert "does not hold everything recorded" in err


def test_other_devices_are_pointed_out_not_assumed(cli, syncthing, capsys):
    """Each device is its own medium; silently counting them would invent
    copies nobody asked about."""
    cli("add-medium", "nas", "--kind", "other", "--durability", "mirror")
    cli("import-syncthing", "nas", *import_args(syncthing, "--folder",
                                                "docs"))
    assert "1 other device(s) share this folder" in capsys.readouterr().err


# ----------------------------------------------------------------- auth

def test_a_rejected_key_says_so_plainly(cli, syncthing):
    cli("add-medium", "nas", "--kind", "other", "--durability", "mirror")
    with pytest.raises(SystemExit) as e:
        cli("import-syncthing", "nas", "--api-url", syncthing["url"],
            "--api-key", "wrong", "--folder", "docs")
    assert "rejected the API key" in str(e.value)


def test_an_unreachable_syncthing_says_where_it_looked(cli):
    with pytest.raises(SystemExit) as e:
        cli("import-syncthing", "--api-url", "http://127.0.0.1:1",
            "--api-key", "k")
    assert "cannot reach Syncthing at http://127.0.0.1:1" in str(e.value)


def test_the_api_key_is_read_from_syncthings_own_config(tmp_path,
                                                        monkeypatch):
    """Nobody should have to go and find it: it is in a known file."""
    monkeypatch.delenv("SYNCTHING_API_KEY", raising=False)
    home = tmp_path / "stconf"
    home.mkdir()
    (home / "config.xml").write_text(
        '<configuration><gui><apikey>from-config</apikey></gui>'
        '</configuration>')
    assert holdings.syncthing_api_key(None, str(home)) == "from-config"


def test_an_explicit_key_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("SYNCTHING_API_KEY", "from-env")
    assert holdings.syncthing_api_key("explicit", None) == "explicit"
    assert holdings.syncthing_api_key(None, None) == "from-env"


def test_no_key_anywhere_explains_where_to_get_one(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNCTHING_API_KEY", raising=False)
    with pytest.raises(SystemExit) as e:
        holdings.syncthing_api_key(None, str(tmp_path / "nothing-here"))
    assert "Actions > Settings > GUI" in str(e.value)
