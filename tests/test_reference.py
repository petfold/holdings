"""Pin docs/REFERENCE.md against the code.

A reference nobody checks is a reference that lies. Every command, every
flag and every enumeration in §4–§7 has to match what the CLI actually has,
so adding a flag without documenting it fails here rather than surprising
someone later.

Deliberately not pinned: prose, defaults and column lists. Testing those
would make the doc hard to write without making it much more honest — the
enumerations are where drift actually hurts.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import holdings  # noqa: E402

REFERENCE = ROOT / "docs" / "REFERENCE.md"

# Documented as `### holdings <name> ...`
HEADING_RE = re.compile(r"^### `holdings ([a-z-]+)", re.M)
FLAG_RE = re.compile(r"`(--[a-z-]+)`")


@pytest.fixture(scope="module")
def cli():
    parser = holdings.build_parser()
    [subs] = [a for a in parser._actions
              if isinstance(a, argparse._SubParsersAction)]
    return parser, subs.choices


@pytest.fixture(scope="module")
def reference():
    return REFERENCE.read_text()


# A section ends at the next heading of any level. Without this the last
# command swallows every section after it, and §9's mention of --exit-code
# looked like a flag of `project-ontodag`.
NEXT_HEADING_RE = re.compile(r"^#{2,3} ", re.M)


def sections(text):
    """Each command's heading mapped to the text under it."""
    out = {}
    for m in HEADING_RE.finditer(text):
        nxt = NEXT_HEADING_RE.search(text, m.end())
        out[m.group(1)] = text[m.start():nxt.start() if nxt else len(text)]
    return out


def test_every_command_is_documented(cli, reference):
    _, commands = cli
    documented = set(sections(reference))
    assert documented == set(commands), (
        f"undocumented: {sorted(set(commands) - documented)}; "
        f"documented but gone: {sorted(documented - set(commands))}")


def test_every_flag_is_documented(cli, reference):
    """Both directions: a new flag must be written down, and a removed one
    must stop being promised."""
    _, commands = cli
    problems = []
    for name, body in sections(reference).items():
        # --json is on every read command and is documented once, in §3;
        # repeating it nine times would be noise rather than honesty.
        real = {o for a in commands[name]._actions for o in a.option_strings
                if o.startswith("--")} - {"--help", "--json"}
        documented = set(FLAG_RE.findall(body)) - {"--json"}
        if missing := real - documented:
            problems.append(f"{name}: undocumented {sorted(missing)}")
        if stale := documented - real:
            problems.append(f"{name}: documented but absent {sorted(stale)}")
    assert not problems, "; ".join(problems)


def test_json_is_on_every_read_command_and_said_once(cli, reference):
    """The reference documents --json globally rather than per command, so
    the claim that it is universal has to actually hold."""
    _, commands = cli
    missing = [n for n, sp in commands.items()
               if sp.get_default("writes") is False
               and not any("--json" in a.option_strings for a in sp._actions)]
    assert missing == ["project-ontodag"], missing
    section = reference[reference.index("## 3. Global flags"):
                        reference.index("## 4. Commands")]
    assert "`--json`" in section


def test_global_flags_are_documented(cli, reference):
    parser, _ = cli
    real = {o for a in parser._actions for o in a.option_strings
            if o.startswith("--")} - {"--help"}
    section = reference[reference.index("## 3. Global flags"):
                        reference.index("## 4. Commands")]
    assert real <= set(FLAG_RE.findall(section)), sorted(real)


def test_the_durability_table_lists_every_class(reference):
    section = reference[reference.index("## 5. Durability classes"):
                        reference.index("## 6. Evidence")]
    for name in holdings.DURABILITY:
        assert f"`{name}`" in section, f"{name} missing from §5"
    counted = re.findall(r"^\| `([a-z]+)` \|.*\| yes", section, re.M)
    assert set(counted) == set(holdings.COUNTS_AS_BACKUP), counted


def test_the_evidence_table_lists_every_level(reference):
    section = reference[reference.index("## 6. Evidence"):
                        reference.index("## 7. Listing formats")]
    for value in ("imported", "metadata", "retrievable", "hashed"):
        assert f"`{value}`" in section, f"{value} missing from §6"


def test_the_listing_formats_match_the_code(reference):
    section = reference[reference.index("## 7. Listing formats"):
                        reference.index("## 8. Schema")]
    documented = set(re.findall(r"^\| `([a-z0-9]+)` \|", section, re.M))
    assert documented == set(holdings.LISTING_FORMATS)


def test_the_schema_table_lists_every_table(reference):
    import sqlite3
    import tempfile
    db = tempfile.mktemp(suffix=".sqlite")
    conn = holdings.db_connect(db)
    try:
        real = {t for (t,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()
        Path(db).unlink(missing_ok=True)
    section = reference[reference.index("## 8. Schema"):
                        reference.index("## 9. Exit codes")]
    assert real <= set(re.findall(r"`([a-z_]+)`", section)), sorted(real)


def test_the_version_matches_the_package(reference):
    # Not tomllib: it arrived in 3.11 and this package supports 3.10.
    m = re.search(r'^version = "([^"]+)"',
                  (ROOT / "pyproject.toml").read_text(), re.M)
    assert m, "pyproject.toml has no version"
    assert f"`{m.group(1)}`" in reference, (
        f"REFERENCE.md does not say it describes {m.group(1)}")


def test_write_commands_are_marked_as_such(cli, reference):
    """The read/write split is load-bearing — write commands refuse a
    published catalog — so the reference must not get it backwards."""
    _, commands = cli
    for name, body in sections(reference).items():
        writes = commands[name].get_default("writes")
        head = body.splitlines()[0]
        assert ("— writes" in head) == bool(writes), head
