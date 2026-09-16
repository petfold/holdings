# holdings — roadmap

Each version has an exit criterion; a version is done when that criterion is
met. Keep this file updated (mark items DONE with a date).

The design contract this all sits under is in the
[README](README.md#design-contract-the-important-part) — observer not
authority, everything regenerable by re-scanning, content hash is identity,
single writer and many readers. Nothing here overrides it.

One clarification that shapes several items below: **how the catalog file
travels is not part of the contract.** A synced folder and a published
read-only copy are both valid, neither is required, and the local file is
authoritative either way. Where Syncthing appears as a *placement source*
(v0.2) that is a different role and unaffected by the choice.

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
      a scan of the synced directory. (Syncthing as a source of facts about
      where files are — independent of what carries the catalog.)

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

## Optional track — publishing the catalog read-only

Orthogonal to the version line above: nothing here gates a version, and
`pip install holdings` stays stdlib-only and complete without any of it.
The point is reach — a phone or a second laptop answering `whereis` against
a catalog published from a machine that has been shut for a week, holding
no copy of the file and paired with nothing. That is the one thing a synced
folder structurally cannot do, because Syncthing needs overlapping uptime.

- [x] **Optional read path** (DONE 2026-09-16): `--db bzz://…` / `bzzf://…`
      opens a published catalog through
      [swarmlite](https://github.com/petfold/swarmlite), behind the
      `holdings[swarm]` extra and imported lazily, so a local catalog never
      touches it. Write commands (`add-medium`, `scan`, `import-restic`)
      refuse a published catalog and point back at the local file —
      single-writer is the contract, not a transport limitation.
- [x] **Index `instances(path)`** (DONE 2026-09-16): the primary key is
      `(medium_id, path)`, so a lookup by path alone scanned every row —
      invisible locally, fatal over a network. Measured on a live node
      against a 125 MB catalog (120k files, 300k placements), cold:
      `whereis sha256:…` 17 pages (0.06%), `whereis <full path>` 8 pages
      (0.03%). Before the index, the path lookup did not finish in 9 minutes.
- [ ] **A bare filename has no index to use.** `resolve_hash` falls back to
      `path LIKE '%/name'`, which a leading wildcard makes unservable — and
      `whereis holiday.jpg` is the README's own headline example. Measured:
      did not finish in 8 minutes over the network. Wants a stored basename
      column with its own index, which is a schema change and a migration,
      hence its own item.
- [ ] **Materialise the backup-copy count on `content`**, refreshed at scan
      time, so `redundancy` and `only-on` become index range scans rather
      than full scans with correlated subqueries. Measured: `redundancy`,
      `only-on`, `stats` and `media` all failed to finish in 8 minutes over
      the network on the catalog above. **Worth doing whatever happens to
      this track** — it speeds the same reports up locally — but it is also
      what decides whether a published catalog is useful for anything beyond
      a point lookup.
- [ ] **Warn on a stale published catalog.** `swarmlite publish` checkpoints
      WAL into the artifact, so `bzz://` and `bzzf://` are always whole; but
      a `file://` read of a live catalog silently skips an un-checkpointed
      WAL (measured: an inserted medium was simply absent). holdings now
      warns when the sidecar is present — see whether the same class of
      staleness needs saying for a feed that has not been republished since
      the last scan.
- [ ] **Publish-side runbook**: `swarmlite publish --encrypt` after a scan
      (the catalog carries filenames, sizes and location hints, so the
      published root is the secret), and `swarmlite stamps --check
      --min-ttl` on a timer. Expiry is survivable here precisely because the
      local file is authoritative and the catalog is regenerable — the
      failure mode is staleness, which the contract already permits.
- [ ] **Swarm as a *medium*** — a different thing from transport: content
      published to Swarm counted as a backup copy by `redundancy`. Needs a
      `sha256 → swarm reference` mapping (a by-product of publishing, so
      exact, unlike `import-restic`'s basename+size matching) and a notion
      of a *leased* copy: a postage batch with three weeks left and a drive
      in a safe are not the same kind of copy, and `--min-copies` cannot say
      so today.

## Future — the browser

Not scheduled; recorded so it is not re-derived later. OntoDAG is intended
to run in the browser eventually, and swarmlite has a JS/SQLite-WASM reader,
so both halves of the placement/semantics join could run client-side against
published roots with no server — which would also subsume v0.5's phone
viewer more cheaply than v0.4's localhost UI.

One wrinkle to solve before that is buildable: the projection contract says
`sys:` memberships are **rebuilt locally on each device** and never synced.
A browser has no local catalog to rebuild from, and `project-ontodag` is a
full scan, so an in-browser rebuild would fetch the whole file. The likely
answer — publishing the projection as a table in the same artifact — is an
amendment to the contract rather than an implementation detail, so it needs
agreeing at the meet point (ontodag `docs/plans/PROJECTIONS.md`), not here.

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
