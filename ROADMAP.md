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
- [x] **Every way of naming a file is now an indexed lookup**
      (DONE 2026-09-16). The primary key is `(medium_id, path)`, so a lookup
      by path alone scanned every row; a bare filename fell back to
      `path LIKE '%/name'`, which a leading wildcard makes unservable by any
      index — and `whereis holiday.jpg` is the README's own headline
      example. Both were invisible locally and fatal over a network (each
      failed to finish in 8–9 minutes). Fixed by `idx_instances_path` and by
      storing the basename in `instances.name` with its own index, migrated
      in place. Measured on a live node, 130 MB published catalog, whole
      command, cold: by hash 17 pages (0.05%), by full path 20 pages
      (0.06%), by bare filename 23 pages (0.07%).
      Cost: the column and its index add ~12% to the catalog on disk, and
      backfilling 300k rows took 16s once.
- [x] **The reports read materialised state instead of scanning**
      (DONE 2026-09-16). `content.copies` / `.backup_copies` /
      `.example_path`, `instances.only_here`, per-medium totals on `media`,
      a one-row `catalog_summary` and a `backup_histogram` for the
      runtime-threshold count — all recomputed wholesale after every write
      command (including `add-medium`, since one `--backup` flag changes
      every count), with `ANALYZE` so the planner has current statistics.
      Measured on a live node, 157 MB published catalog, whole command,
      cold: `stats` 4 pages, `media` 4, `redundancy` 56, `only-on` 71 —
      all four of which previously failed to finish in 8 minutes.
      Cost: the derived columns and their indexes grew the catalog from
      113 MB to 157 MB (+39%), and refresh adds a few seconds to a scan.
- [ ] **`diff A B` is the one report still proportional to its input.** It
      asks "does B hold this too?" once per file on A, which no
      precomputation removes. Acceptable locally; expensive over a network.
      If it ever matters, it wants a per-pair summary rather than a better
      index.
- [x] **Warn on a stale published catalog** (DONE 2026-09-16). A synced
      folder refreshes itself; a pin never does and a feed only moves when
      someone republishes, so a reader can be looking at months-old
      placement with nothing on screen to say so. Reads of a published
      catalog now warn when its newest scan is older than `--max-scan-age`
      (default 30 days). Note this measures *scan* age, not publication
      age: the obvious check — the feed's last update against the catalog's
      newest scan — says nothing, because a catalog is always written
      before it is published, so that gap is small and reassuring even when
      the writer stopped scanning a year ago. A reader genuinely cannot
      detect "scanned but not published"; only the writer can, which is why
      that half lives in the runbook as a habit rather than a check.
- [x] **Publish-side runbook** (DONE 2026-09-16):
      [docs/PUBLISHING.md](docs/PUBLISHING.md) — the scan/publish loop, why
      `--encrypt` is not optional for a file carrying filenames and location
      hints, feeds versus pins, postage renewal as a cron line, and what a
      lapsed batch does and does not cost.
- [ ] **Swarm as a *medium*** — a different thing from transport: content
      published to Swarm counted as a backup copy by `redundancy`. Needs a
      `sha256 → swarm reference` mapping, which is a by-product of
      publishing and therefore exact, unlike `import-restic`'s
      basename+size matching.

      The modelling problem is the copy itself. Every other medium fails by
      **event** — a drive dies, a laptop is stolen. A stamped copy fails by
      **inaction**, on a schedule, and `--min-copies 2` cannot say that one
      of two copies evaporates in three weeks. So a leased copy needs:

      * a stored expiry **and** the time the estimate was taken. A node's
        TTL is derived from the batch balance at the *current* storage
        price; if the price rises the batch drains faster and expiry
        arrives sooner than quoted. The figure is an optimistic bound that
        also goes stale where it sits — "18 days left", recorded four
        months ago, is an expired lease, not an 18-day one.
      * a **conservative** reading everywhere it is used: discount the
        estimate, and treat an estimate older than its own remaining life
        as expired rather than trusted.
      * `redundancy` must not count a lease that is about to lapse as a
        backup copy. Getting this wrong is the failure this tool exists to
        prevent — `only-on` is the list people wipe drives from.

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
