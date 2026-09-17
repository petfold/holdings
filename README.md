# holdings — the placement catalog

A disposable, regenerable catalog answering: **which media hold which
files?** — and the two questions that follow from it: *is this backed up?*
and *is it safe to wipe this?*

Single file, stdlib only, Python 3.10+. SQLite for placement truth;
semantics belong to OntoDAG (see *Projection contract*, below).

```bash
pip install holdings

holdings add-medium laptop-x1      --kind laptop --site home
holdings add-medium drive-budapest --kind drive --durability independent \
                                   --site budapest --location "safe, Budapest"
holdings scan laptop-x1 /home/you
holdings scan drive-budapest /media/you/backup-drive

holdings whereis holiday.jpg                  # which media hold it
holdings redundancy --min-copies 2 --exit-code    # the policy, as a check
holdings redundancy --on laptop-x1 --min-copies 1 # safe to reformat?
holdings due                                  # what to re-read next
```

## Documentation

| | |
|---|---|
| **[User Guide](docs/USER_GUIDE.md)** | how to use it, in the order you will want it |
| **[Reference](docs/REFERENCE.md)** | every command, flag, class and column — pinned against the code by the test suite |
| **[Publishing](docs/PUBLISHING.md)** | putting a catalog where other devices can query it |
| **[Roadmap](ROADMAP.md)** | what is done, what is not, and why |

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


## What it is careful about

The design is mostly a list of wrong answers it refuses to give.

**A sync mirror is not a backup.** A file on your laptop and in Dropbox has
two copies and no backup: one `rm` removes both. Every medium declares what
its copies survive — `working`, `independent`, `mirror`, `leased`,
`hosted` — and only the last three count. See the
[User Guide §2](docs/USER_GUIDE.md#2-say-what-each-medium-survives).

**Seen is not verified.** A rescan reuses the stored hash without opening
the file, and bit rot changes neither size nor mtime, so a copy can be
catalogued faithfully for years and be gone. Instances record how they are
known, and `--verified-within` discounts evidence nobody has refreshed.

**Two drives in one drawer are not two sites.** `--min-sites` and
`--min-kinds` make the 2 and the 1 of 3-2-1 checkable, not just the 3.

**A copy that expires is not a copy that does not.** A lease is counted while
it holds and reported when it lapses; hosting nobody else participates in —
a Radicle repo seeded only by your own node — does not count at all.

**An unreadable file is not a deleted one.** A failing drive and a tidied-up
one must not produce the same catalog change, so unreadable paths are kept
and reported rather than pruned.

## Beyond mounted media

Anything that can list itself can be recorded: restic, rclone, S3, a
`sha256sum` run over ssh, a Syncthing cluster, a git remote (GitHub, Hugging
Face, Radicle), a Swarm root. Where the source can supply a SHA-256,
placement is exact rather than matched by name and size. See
[User Guide §7](docs/USER_GUIDE.md#7-media-you-cannot-mount).

The catalog can also be published read-only and queried from a phone without
holding it — a lookup fetches tens of kilobytes of a catalog that may be
hundreds of megabytes — or served offline from a checkout with
`python web/serve.py`. See [Publishing](docs/PUBLISHING.md).

## Projection contract (OntoDAG integration)

*(2026-08-20: this contract's canonical statement now lives at the meet
point — [ontodag `docs/plans/PROJECTIONS.md`](https://github.com/petfold/ontodag/blob/main/docs/plans/PROJECTIONS.md)
— which generalizes it across sources (files here, messages in ucomm)
and adds retention classes. The rules below remain the agreed file-side
instance and the wire format is unchanged.)*

```bash
holdings project-ontodag --out placement.jsonl
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

## Notes & limits

* A **listing** proves paths and sizes, never bytes. Entries are matched to
  known content when that is unambiguous, recorded as `unverified:`
  otherwise, and taken exactly when the source supplies a SHA-256. Mounting
  the source and scanning it is the only thing that proves content.
* Symlinks are skipped. Hidden config and caches are excluded by default
  (`.cache`, `.config`, `.git`, `node_modules`, Syncthing internals, …);
  add your own with `--exclude-file`.
* Concurrent writes are not supported, by design: single writer, many
  readers.
* `diff A B` is proportional to what is on A, unlike the other reports.
* holdings cannot verify a durability class or a site. Those are your
  assertions when you register a medium; what it can do is stop one word
  standing in for five different things.
* Tests: `pip install -e ".[test]" && pytest` — **241 tests**, stdlib only,
  no node and no network; a guard fails if that number drifts from the
  suite. The browser viewer's logic has its own:
  `node --test web/test/view.test.mjs`.
* Roadmap: see [ROADMAP.md](ROADMAP.md).
