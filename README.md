# holdings — v0.1 of the placement catalog

A disposable, regenerable catalog answering: **which media hold which files?**
Single file, stdlib only, Python 3.10+. SQLite for placement truth; semantics
belong to OntoDAG (see *Projection contract* below).

## Design contract (the important part)

1. **Observer, not authority.** holdings only *reads* filesystems and backup
   listings. It never writes to your data, never sits in the backup or sync
   path. Deleting holdings and its database costs nothing but convenience.
2. **Everything is regenerable by re-scanning.** The catalog is a cache of
   facts about the world. The only original data in the whole system is your
   *human* OntoDAG categorization — which holdings never touches.
3. **Content hash is identity.** `sha256:…` is the primary key everywhere.
   Paths, media, snapshots, categories: all attributes of a hash.
4. **Single writer, many readers.** Scan on the backup-node laptop; every
   other device only reads. How the catalog reaches them is *not* part of
   the contract — put the SQLite file in a synced folder (Syncthing, or
   anything else) and every device carries the full index of everything,
   including drives offline in another country. Or publish it read-only
   and have them query it without holding it at all (see *Reading a
   published catalog*, below). Both work; neither is required.

## Quick start

```bash
export HOLDINGS_DB=~/Sync/catalog/catalog.sqlite   # put it in a synced folder

# Register your media once:
./holdings.py add-medium laptop-x1       --kind laptop --location "with me"
./holdings.py add-medium drive-budapest  --kind drive --backup --location "safe, Budapest"
./holdings.py add-medium drive-standrews --kind drive --backup --location "office, St Andrews"
./holdings.py add-medium restic-b2       --kind restic-repo --backup

# Scan whenever a medium is mounted (fast on rescan: unchanged files
# are recognized by size+mtime and not rehashed):
./holdings.py scan drive-budapest /media/peter/backup-drive
./holdings.py scan laptop-x1 /home/peter --exclude-file ~/backup/excludes.txt

# Count backup snapshots as copies (approximate matching by name+size;
# for exact hashes, `restic mount` the repo and `scan` it instead):
restic -r b2:bucket:repo ls --json latest | ./holdings.py import-restic restic-b2 -
```

## Queries

```bash
./holdings.py whereis holiday.jpg        # every medium+path holding this content
./holdings.py whereis sha256:45887c...   # by hash
./holdings.py redundancy --min-copies 2  # content below 2 backup copies
./holdings.py only-on drive-budapest     # DANGER LIST: exists nowhere else
./holdings.py diff drive-a drive-b       # on A but not B
./holdings.py media                      # media overview
./holdings.py stats                      # totals
```

`redundancy` turns your 3-2-1 policy into a checkable report.
`only-on` is the consolidation to-do list for old scattered drives: run it,
back those files up via restic, rescan, watch the list empty, then wipe the
drive with confidence.

## Reading a published catalog (optional)

The catalog is a path, so distributing it is someone else's job — and the
default, a file in a synced folder, needs nothing installed. The alternative
is to publish it read-only and let other devices *query* it instead of
carrying it:

```bash
pip install 'holdings[swarm]'        # optional; a local catalog needs no extra

# On the scanning laptop, after a scan — publishing is a separate tool:
swarmlite publish ~/catalog.sqlite --encrypt --feed holdings --signer $KEY

# Anywhere else, with no copy of the file and no device pairing:
holdings --db bzzf://<owner>/holdings/catalog.sqlite whereis holiday.jpg
```

[swarmlite](https://github.com/petfold/swarmlite) maps SQLite's 4 KB pages
onto Swarm range reads, so an indexed lookup fetches a handful of pages
rather than the catalog — a phone can answer `whereis` against a laptop that
has been shut for a week. `--encrypt` matters: a catalog carries filenames,
sizes and location hints, and the published root becomes the secret.

Two things this deliberately is not:

* **Not required.** `pip install holdings` stays stdlib-only and complete.
  The reader is imported lazily and only when a `--db` URL asks for it, so
  a local catalog never pays for it and someone who never wants Swarm sees
  none of it.
* **Not a write path.** Published catalogs are read-only; `add-medium`,
  `scan` and `import-restic` refuse a URL and point back at the local file.
  Single-writer is the design contract, not a limitation of the transport.

Worth knowing before leaning on it. Measured against a live Bee node on a
130 MB published catalog (120k files, 300k placements) — whole command,
cold cache each time:

| query | pages fetched | of the file | time |
|---|---|---|---|
| `whereis sha256:…` | 17 | 0.05% | 14.6s |
| `whereis <full path>` | 20 | 0.06% | 11.0s |
| `whereis <bare filename>` | 23 | 0.07% | 1.8s |
| `redundancy`, `only-on`, `stats`, `media` | — | did not finish in 8 min | |

So lookups are what a published catalog is good for today — three orders of
magnitude less than the file, on any of the three ways of naming a file. The
reports are still full scans and remain a roadmap item
([ROADMAP.md](ROADMAP.md)), worth fixing locally regardless of Swarm.

## Projection contract (OntoDAG integration)

*(2026-08-20: this contract's canonical statement now lives at the meet
point — [ontodag `docs/plans/PROJECTIONS.md`](https://github.com/petfold/ontodag/blob/main/docs/plans/PROJECTIONS.md)
— which generalizes it across sources (files here, messages in ucomm)
and adds retention classes. The rules below remain the agreed file-side
instance and the wire format is unchanged.)*

```bash
./holdings.py project-ontodag --out placement.jsonl
```

Emits JSON lines: `{"item": "<hash>", "supercategories": ["sys:on:<medium>",
"sys:type:<ext>", "sys:backup:<n>"]}` — matching OntoDAG's
`put(item, supercategories)` model.

The agreed rules for the ingesting side:

* everything under the **`sys:` namespace is machine-written, regenerable
  cache** — never hand-edit, never treat as authoritative;
* ingestion is an **idempotent full rebuild**: drop all `sys:` memberships,
  re-ingest the stream (no incremental diffing — staleness is the only
  permitted failure mode, drift is not);
* the projection is **rebuilt locally on each device** from the synced
  SQLite, not synced itself; only the human layer of the DAG is persisted
  and synced (it is the irreplaceable original data — back it up like the
  KeePass vault);
* human categories are attached to the same hash-identified items, giving
  unified queries like `get(photo, vienna, sys:on:drive-budapest)`.

See `ontodag_ingest.py` for an adaptation template.

## Notes & limits (v0.1)

* `import-restic` matches listing entries to known content by basename+size
  and only when unique; ambiguous entries are recorded as
  `unverified:` placeholders. Scanning a `restic mount` gives exact hashes.
* Symlinks are skipped. Hidden config/caches are excluded by default
  (`.cache`, `.config`, `.git`, `node_modules`, Syncthing internals, …);
  add your own with `--exclude-file`.
* Concurrent writes are not supported by design (single-writer model).
* Tests: `pip install -e ".[test]" && pytest` — **69 tests**, stdlib only, no
  node and no network; a guard fails if that number drifts from the suite.
* Roadmap: see [ROADMAP.md](ROADMAP.md) — v0.2 through v0.5, and which of the
  limits above are meant to change.
