# holdings — roadmap

Each version has an exit criterion; a version is done when that criterion is
met. Keep this file updated (mark items DONE with a date).

The design contract this all sits under is in the
[README](README.md#design-contract-the-important-part) — observer not
authority, everything regenerable by re-scanning, content hash is identity,
single writer and many readers. Nothing here overrides it.

---

## v0.1 — the placement catalog

Goal: answer "which media hold which files?" from a single stdlib-only script.

- [x] `holdings` CLI over SQLite (DONE 2026-09): `add-medium`, `scan`,
      `import-restic`, `whereis`, `redundancy`, `diff`, `only-on`, `stats`,
      `project-ontodag`.
- [x] Rescan is cheap: unchanged files recognised by size+mtime, not rehashed.
- [x] Packaged and published as `holdings` 0.1.0 (DONE 2026-09-10).
- [x] `ontodag_ingest.py` as an adaptation template for the projection
      contract (DONE 2026-09).

## v0.2 — Syncthing REST adapter

- [ ] Read placement from Syncthing's REST API rather than inferring it from
      a scan of the synced directory.

## v0.3 — OntoDAG join live

- [ ] Consume ontodag's overlay view directly instead of emitting a JSONL
      projection for someone else to ingest. The wire format is
      ontodag's `docs/plans/PROJECTIONS.md` §4; `ontodag_ingest.py` is the
      pre-API template of this.
- [ ] Retention-aware redundancy join — the enforcement report as the join of
      `redundancy` / `diff` against the categorisation.

## v0.4 — localhost UI

- [ ] FastAPI localhost UI over the same database.

## v0.5 — read-only viewer for phones

- [ ] WASM/PWA read-only viewer, reading the synced SQLite file directly so a
      phone needs no server.

---

## Known limits, and whether they are roadmap

From the README's "Notes & limits", separated by whether they are meant to
change:

- [ ] **`import-restic` matching is approximate** — listing entries are
      matched by basename+size and only when unique; ambiguous ones are
      recorded as `unverified:` placeholders. Exact hashes already work via
      scanning a `restic mount`, so the fix is a first-class restic reader.
- **Symlinks are skipped** — by design, not roadmap.
- **Single-writer** — by design (the contract above), not roadmap.
- **Hidden config and caches excluded by default** — by design; `--exclude-file`
  covers the rest.
