#!/usr/bin/env python3
"""
ontodag_ingest.py — adaptation template for ingesting datacat's placement
projection into an OntoDAG instance.

This is a TEMPLATE: adapt the three marked functions to OntoDAG's actual API
(github.com/petfold/ontodag). The contract it implements:

  1. All `sys:*` categories are regenerable cache. Ingestion drops every
     existing sys: membership and rebuilds from the projection stream
     (idempotent full rebuild — staleness allowed, drift not).
  2. Human categories (anything not under `sys:`) are never touched.
  3. Items are identified by content hash — the join key with SQLite.

Usage:
    ./datacat.py project-ontodag | python3 ontodag_ingest.py mydag.pkl
"""

import json
import sys

# --- adapt these three to OntoDAG's real API -------------------------------

def load_dag(path):
    from ontodag import OntoDAG          # adjust import to your package layout
    return OntoDAG.load(path)            # or however persistence works today


def drop_sys_layer(dag):
    """Remove every category under the sys: namespace and all memberships
    in them. If OntoDAG grows namespace support (roadmap), this becomes a
    one-call subtree removal; until then, iterate and remove()."""
    for name in [n for n in dag.names() if n.startswith("sys:")]:
        dag.remove(name)


def put(dag, item, supercategories):
    """Insert item under supercategories, creating missing sys: categories.
    Suggested root structure:  sys: > sys:on:*, sys:type:*, sys:backup:*
    so the whole projection hangs under one removable node."""
    for sc in supercategories:
        if not dag.contains(sc):
            parent = "sys:" + sc.split(":")[1]      # e.g. sys:on
            dag.put(sc, {parent})
    dag.put(item, set(supercategories))

# ---------------------------------------------------------------------------


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: project-ontodag output on stdin;"
                 "  ontodag_ingest.py <dag-file>")
    dag = load_dag(sys.argv[1])

    # bootstrap the sys: scaffold
    for root in ("sys:", "sys:on", "sys:type", "sys:backup"):
        if not dag.contains(root):
            dag.put(root, {"sys:"} if root != "sys:" else set())

    drop_sys_layer(dag)      # idempotent rebuild starts from a clean slate
    n = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        put(dag, rec["item"], rec["supercategories"])
        n += 1
    dag.save(sys.argv[1])
    print(f"rebuilt sys: projection: {n} items", file=sys.stderr)

    # Differential test idea (recommended): after rebuild, verify DAG
    # invariants and spot-check that get({"sys:on:<medium>"}) matches
    # SELECT hash FROM instances WHERE medium_id=<medium> in SQLite.
    # SQLite is ground truth; disagreements are OntoDAG bugs found for free.


if __name__ == "__main__":
    main()
