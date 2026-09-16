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

## v0.4 — scriptable, and servable offline

Was "FastAPI localhost UI over the same database". Dropped, because the
premise did not survive the question *who would use the API?*

- Another program on this machine opens the catalog directly — it is a
  plain SQLite file. An HTTP server in front of it adds a port, a process
  and a dependency to reach data that was already open.
- Remote consumers are served by a published catalog (no server at all,
  and it answers while the machine is off).
- The browser viewer talks SQL to the file through its VFS; it never
  wanted an API, which is why v0.5 shipped without one.
- Scripts were the one real audience — and a flag serves them better than
  a server.

- [x] **`--json` on every read command** (DONE 2026-09-16): one object per
      invocation, composes with `jq`, works over ssh, needs nothing
      running. A structural test fails if a read command is added without
      it.
- [x] **`--exit-code` on `redundancy` and `only-on`** (DONE 2026-09-16).
      The README has called `redundancy` a checkable report since v0.1,
      but it exited 0 whatever it found, so nothing could check it. Now
      `redundancy --min-copies 2 --exit-code` is a cron line, and
      `only-on <drive> --exit-code` is the gate to put in front of wiping
      one — the workflow the README already describes.
- [x] **Serve the viewer locally** (DONE 2026-09-16), as `web/serve.py`
      rather than a `holdings serve` subcommand. A packaged
      `pip install holdings` contains neither `web/` nor swarmlite's
      JavaScript, so a subcommand would have been a promise the install
      cannot keep; this sits beside `web/publish.py`, which already solves
      locating the reader the same way. Promoting it to a subcommand is a
      packaging question, not a code one.

      Stdlib only, including the catalog snapshot (sqlite3's backup API,
      so it is consistent while a scan writes and never touches the
      original) — swarmlite's Python package is not needed, only its
      JavaScript, which is copied as files.

      It serves a snapshot rather than the live file, because the reader
      cannot see a WAL sidecar and would read a live catalog stale with no
      warning. The Range handler is the one piece `http.server` does not
      provide: it ignores `Range` and answers 200 with the whole file, which
      would make the local case worse than the published one.

- [ ] **A writer's console** — a local UI that can drive `scan`,
      `add-medium` and `import-restic`. Deliberately separate: it would
      break the read-only-by-construction property that currently makes
      the viewer safe to hand to anyone, so it is a different product and
      a different decision.

## v0.5 — read-only viewer for phones

- [x] **Read-only viewer** (DONE 2026-09-16), in `web/`: media, `whereis`,
      `redundancy` and `only-on` as a static page over SQLite-WASM, with
      `web/publish.py` to assemble and publish it.

      Done before v0.4 deliberately. The original wording was "reading the
      synced SQLite file directly so a phone needs no server" — but a phone
      does not need the file either. Reads go page by page over range
      requests: opening the viewer on a 157 MB catalog fetches 5 pages
      (0.014%), and the heaviest report 71 (0.200%), identical to the CLI's
      counts. That only became true once the reports stopped scanning, which
      is why this lands after that work rather than before it.

      The SQL lives in `holdings.QUERIES`, is generated into
      `web/queries.json`, and a test fails when the committed copy drifts —
      a second hand-written copy of those queries would have gone stale the
      day the aggregates moved to write time.

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
- [x] **What does a copy survive?** (DONE 2026-09-16) — `media.durability`
      replaces the asserted `is_backup` boolean, which is now derived from
      it. Classes: `working`, `independent`, `mirror`, `leased`, `hosted`.
      Only the last three count toward `redundancy`; a sync mirror does
      not, because your deletion reaches it.

      Urgent once `--exit-code` shipped: that turned `redundancy` into a
      gate scripts trust, and the gate was only as good as an unverified
      boolean covering five different failure modes. A file on a laptop and
      in Dropbox now reads as two copies and no backups.

      Leases carry an expiry **and** the time the estimate was taken, and
      are evaluated at read time rather than materialised — a lease lapses
      with no write happening anywhere, so a stored answer would go quietly
      wrong between scans. `--lease-margin` (default 14 days) sets the
      headroom, since a node's TTL is an optimistic bound at the current
      storage price. An aged estimate is reported rather than trusted.
      Migration reads an existing `is_backup=1` as `independent`, which is
      what it claimed.

      Still open, and each now has somewhere to live:

      * [x] **Swarm as a medium** (DONE 2026-09-16): `import-swarm`, and
        `add-medium --lease-from-batch` so the expiry is read from the
        postage batch rather than typed — the figure moves, and a typed one
        goes stale silently. `instances.external_ref` holds the Swarm
        reference beside the content hash: identity stays the hash, the
        reference is an address, and it is what will make a remote copy
        checkable without downloading it.

        Placement is as exact as the evidence allows, not more: a manifest
        listing gives paths and sizes, so entries match known content where
        unambiguous and are `unverified:` otherwise, exactly as with restic.
        Mounting the root and scanning it remains the way to get bytes.
        Verified live against a node: a 138.8 MB published root imported,
        reference recorded, and `due` then ranked it first because nothing
        had ever read it.

      * [ ] **Check a Swarm copy without downloading it** — the reference is
        stored now, and Bee can be asked whether content is still
        retrievable. That would make a leased copy the first remote medium
        that can actually be verified rather than merely trusted.

      * **A generic listing importer** — restic, S3/B2, rclone and Swarm
        are one shape (a listing of paths and sizes), not four readers.
      * **Hubs** — needs git-awareness, not a directory scan: only content
        committed *and* pushed *and* still reachable from a remote ref is
        there. `hosted` is the class; the reader is the work. Radicle sits
        in the same class but its countable quantity is seeds *other than
        your own* — a repo seeded only by your node is not a second copy.
      * **Sync mirrors as a placement source** — v0.2's Syncthing REST
        adapter feeds `mirror`, and the class is now there to receive it.

- [x] **Sites: what fails together** (DONE 2026-09-16). `media.site`, plus
      `content.backup_sites` / `backup_kinds`, and `redundancy --min-sites`
      / `--min-kinds`. The README had called this a 3-2-1 report since v0.1
      while checking only the 3; the 2 was derivable from `kind` but never
      tested, and the 1 had nowhere to live, `location_hint` being free
      text. Unsited media collapse into one unknown site — under-counting
      separation is the safe direction. The default question keeps its
      index; the full form needs an OR across columns, so it costs a scan
      and only runs when asked for.
- [x] **Seen is not verified** (DONE 2026-09-16). `instances.verified_at`
      and `evidence` (hashed / metadata / imported), `media.verified_at`,
      `content.backup_verified_at`, and `redundancy --verified-within`.
      A rescan reuses the stored hash without opening the file and bit rot
      changes neither size nor mtime, so a copy could be faithfully
      catalogued for years and be gone. A `--full` scan now reports a path
      that hashed differently instead of swallowing it, and an unreadable
      file is kept and flagged rather than pruned as deleted — a failing
      drive and a tidied-up one used to produce the same catalog change.

      Evidence decays the way a lease does. That is the same shape twice
      now, and it may deserve stating once: the catalog treats a fact from
      five years ago exactly like one from yesterday unless something says
      otherwise.
- [x] **A verification schedule** (DONE 2026-09-16): `holdings due`.
      `--verified-within` asked which content rests on stale evidence but
      said nothing about what to do, and the answer is per-medium — you dig
      one drive out of the safe and read it.

      `media.sole_backup_count` / `sole_backup_bytes` is the new number:
      content whose *only* backup copy is on that medium, which is what
      re-reading it would protect. Distinct from `only_here_*`, where a
      working copy may exist but nothing would survive deleting it.

      Ordering is explainable rather than weighted — sole backups first,
      then longest unread — because the output is a plan someone acts on.
      Read-only, and everything it needs is already on the media row, so it
      is a handful of pages even against a published catalog: "what should
      I dig out of the safe next?" is a question worth being able to ask
      from a phone.

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
