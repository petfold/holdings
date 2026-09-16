"""Documentation guards: claims in the docs that a test can check."""


def test_readme_states_the_current_test_count(request):
    """The README quotes a test count, and a quoted number goes stale in silence.

    Enforced **only in CI**, because the collected count depends on which
    optional dependencies are installed and CI is the one reproducible
    environment (`pip install -e ".[test]"` on a clean runner). A developer
    machine with extra packages present — or missing one — collects a
    different number through no fault of the docs, so failing there would be
    noise. Publication is gated on CI, which is where this needs to hold.
    """
    import os
    import re
    from pathlib import Path

    import pytest

    if not os.environ.get("CI"):
        pytest.skip("enforced in CI, where the environment is canonical")
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    m = re.search(r"(\d{2,4}) (?:tests|passed)", readme)
    assert m, "the README no longer quotes a test count — drop this guard, or restore it"
    claimed, collected = int(m.group(1)), request.session.testscollected
    assert collected == claimed, (
        f"README says {claimed} tests, CI selects {collected}. "
        "Update the README (this is the reminder that docs drift silently)."
    )


def test_the_browser_viewer_runs_the_same_sql_as_the_cli():
    """`web/queries.json` is generated from holdings.QUERIES.

    The viewer is a second reader of the same published file. Every report
    query in it has already been rewritten once — when the aggregates moved
    to write time — and a hand-copied duplicate would have gone stale that
    day, reporting backup counts the CLI had stopped believing.
    """
    import json
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    import holdings

    committed = json.loads((root / "web" / "queries.json").read_text())
    assert committed == holdings.QUERIES, (
        "web/queries.json is out of date with holdings.QUERIES — regenerate:\n"
        "  python -c \"import json, holdings, pathlib; "
        "pathlib.Path('web/queries.json').write_text("
        "json.dumps(holdings.QUERIES, indent=2) + chr(10))\""
    )


def test_no_read_query_writes():
    """The viewer has no write path at all, so the shared queries must not
    contain one — a published catalog would reject it, but the failure
    should be visible here rather than as a runtime error in a browser."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import holdings

    forbidden = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE")
    for name, sql in holdings.QUERIES.items():
        upper = sql.upper()
        assert not any(w in upper for w in forbidden), f"{name} is not read-only"
