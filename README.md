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

### Importing from anything that can list itself

restic, rclone, an S3 bucket, a Swarm manifest, a machine you can only
reach over ssh — all the same shape: paths and sizes, sometimes with a hash
or an address alongside. One command reads them:

```bash
# rclone, and on backends that can produce SHA-256 the placement is exact:
rclone lsjson -R --hash remote:path | ./holdings.py import b2 - --format rclone

# a machine you cannot mount, catalogued exactly, nothing installed at the far end:
ssh nas 'cd /data && find . -type f -exec sha256sum {} +' \
    | ./holdings.py import nas - --format sha256sum

# anything else, via the escape hatch: {path, size, hash?, reference?}
./holdings.py import mymedium listing.jsonl
```

The report keeps three different claims apart, because they are worth
different amounts:

```
imported 412 entries into 'nas': 412 by hash (exact),
  0 matched by name+size, 0 unverified
```

**by hash** is exact — the listing supplied a sha256. **matched by
name+size** is a guess that happened to be unique. **unverified** is a
placeholder that keeps the file visible without pretending to know what it
is; those carry the medium's name, so the same unidentified file on two
media stays two objects rather than being assumed shared, which understates
redundancy instead of overstating it.

A listing never proves bytes. Mounting the source and scanning it still is
the only thing that does.

## Queries

```bash
./holdings.py whereis holiday.jpg        # every medium+path holding this content
./holdings.py whereis sha256:45887c...   # by hash
./holdings.py redundancy --min-copies 2  # content below 2 backup copies
./holdings.py only-on drive-budapest     # DANGER LIST: exists nowhere else
./holdings.py diff drive-a drive-b       # on A but not B
./holdings.py media                      # media overview
./holdings.py stats                      # totals
./holdings.py due                        # what to re-read next, most urgent first
./holdings.py check                      # derived columns vs. the base tables
```

### Scripting it

Every read command takes `--json` and emits one object, so nothing needs a
server to consume this — the catalog is a plain SQLite file, and now its
reports are structured output:

```bash
./holdings.py stats --json | jq .content
./holdings.py only-on drive-old --json | jq -r '.items[].path'
```

`redundancy` and `only-on` also take `--exit-code`, which turns them into
checks a timer can run:

```bash
# 3-2-1 policy, enforced rather than merely reported:
holdings redundancy --min-copies 2 --exit-code || notify-send 'holdings: under-backed'

# the gate to put in front of wiping an old drive:
holdings only-on drive-old --exit-code || wipe-it
```

`redundancy` turns your 3-2-1 policy into a checkable report.
`only-on` is the consolidation to-do list for old scattered drives: content
that exists on one medium and nowhere else at all.

### Before you wipe something

"Is everything on this laptop backed up?" is **not** what `only-on` answers,
and the difference can bite. `only-on` asks *does anything else hold this?* —
and a sync mirror answers yes, while being exactly the copy that does not
survive. A file on your laptop and in Dropbox is not `only-on` the laptop,
and has no backup at all.

Ask about backups instead, scoped to the medium you are about to destroy:

```bash
./holdings.py redundancy --on laptop-x1 --min-copies 1 --exit-code || echo "not yet"
```

That is the reformat gate: it counts only copies that survive deleting the
original, and it ignores everything not on that medium, so an unbacked file
on some other drive does not block you. Raise `--min-copies` if one backup
is not enough to bet on.

## What counts as a backup copy

`--backup` used to be a claim you made, and `redundancy` believed it. Since
that report is now a gate scripts run (`--exit-code`, above), the question
lives in the data instead. Every medium has a **durability class**, and the
test each answers is: *if the original is deleted, does this copy survive,
and for how long?*

| class | survives deletion? | examples |
|---|---|---|
| `working` | no — it *is* the original | the laptop you edit on |
| `independent` | yes, until it fails itself | second drive, restic repo |
| `mirror` | **no — your deletion propagates** | Syncthing, Dropbox, Drive |
| `leased` | yes, until the lease lapses | Swarm postage, prepaid storage |
| `hosted` | yes, while someone else allows it | GitHub, Hugging Face, Radicle |

```bash
./holdings.py add-medium drive-budapest --kind drive --durability independent
./holdings.py add-medium dropbox        --kind cloud --durability mirror
./holdings.py add-medium swarm          --kind cloud --durability leased \
                                        --lease-expires 30d
```

`--backup` still works and means `--durability independent`.

Only `independent`, `leased` and `hosted` count toward `redundancy`. A sync
mirror does not: it protects against losing a device and nothing else, so a
file on your laptop and in Dropbox has **two copies and no backups** — which
is what `redundancy` now says, and what one `rm` would have proved.

A lease counts while it holds, and is checked at read time rather than
stored, because a lease lapses with no write happening anywhere:

```
$ holdings redundancy --min-copies 2
AT RISK: leased medium 'swarm' -- 9 days left
```

`--lease-margin DAYS` (default 14) sets how much headroom a lease needs. The
margin matters because a node's TTL is an estimate at the *current* storage
price: if the price rises the batch drains faster than quoted, so the figure
is an optimistic bound. holdings stores the estimate *and* when it was
taken, and says so when an estimate has aged.

holdings still cannot verify a class — it is your assertion when you
register a medium. What it can do is stop one word standing in for five
different things.

### Sites: what fails together

Durability says what kills one copy. It does not say what kills several at
once, and two drives in the same drawer as the laptop share fire, flood,
theft and a ransomware process walking every mounted volume. Counted as two,
they are closer to one.

```bash
./holdings.py add-medium drive-home --kind drive --durability independent --site home
./holdings.py add-medium drive-bp   --kind drive --durability independent --site budapest

# the whole of 3-2-1, not just the 3:
./holdings.py redundancy --min-copies 2 --min-sites 2 --min-kinds 2 --exit-code
```

Media with no `--site` recorded collapse into a single unknown site rather
than each counting separately: unknown is not the same as known-different,
and under-counting separation is the safe direction.

### Seen is not verified

`seen_at` means the filesystem still listed the file at that size. It does
**not** mean anyone read it: a rescan reuses the stored hash without opening
the file, and bit rot changes neither size nor mtime. So a copy can be
faithfully catalogued for years and be gone.

Every instance therefore also records `verified_at` and how it is known —
`hashed` (the bytes were read), `metadata` (the filesystem said so), or
`imported` (a listing matched on name and size, the weakest of the three).

```bash
./holdings.py scan drive-bp /media/you/bp --full   # re-read and re-verify
./holdings.py redundancy --min-copies 2 --verified-within 365 --exit-code
```

`due` turns that into a plan. `--verified-within` says which *content* is
backed only by evidence nobody has refreshed; it does not say what to do,
and the answer is per-medium — you dig one drive out of the safe and read
it. `due` orders the media by how much that is worth:

```
$ holdings due
MEDIUM                 LAST READ       AGE   SOLE BACKUP FOR   TO RE-READ  WHERE
!drive-bp              2025-11-20     300d        2 (1.4 GB)       1.4 GB  budapest
 drive-home            2026-09-16       0d          1 (7 MB)         7 MB  home

1 medium(s) marked ! have not been read in 180 days.
```

*Sole backup for* is what re-reading would actually protect: content whose
only backup copy is there. Ordering is deliberately explainable rather than
a weighted score — media holding the only backup of something first, then
the longest unread — because the output is a plan someone acts on, and a
clever ranking nobody can predict is worse than a dull one.
`--stale-after DAYS` (default 180) sets the threshold, and `--exit-code`
makes it a cron line.

A `--full` scan that finds different bytes at a known path now says so
instead of quietly replacing the hash — on a medium nobody edits, that is
what rot looks like. And a file that cannot be read is kept and reported
rather than pruned as deleted, because a failing drive and a tidied-up one
must not produce the same catalog change.

### Syncthing

A synced folder is scannable as an ordinary path, so why ask Syncthing?
Because a scan tells you what *this* machine holds, and Syncthing already
knows what the whole cluster holds and how complete each device is — facts
about machines you cannot scan.

```bash
./holdings.py import-syncthing                      # which folders exist?
./holdings.py add-medium nas --kind other --durability mirror --site attic
./holdings.py import-syncthing nas --folder docs
```

The API key is found in Syncthing's own config if you do not pass one.

Two things it is careful about. `db/browse` returns the **cluster's index** —
what the devices agree the folder contains — not one device's disk, so a
device below 100% is flagged rather than credited with copies it has not
finished receiving. And other devices sharing the folder are pointed out,
never counted: each is its own medium, and silently assuming them would
invent copies nobody asked about.

Everything it records is a `mirror`. Two copies, no backups — which is what
`redundancy --on laptop` will tell you, and it is right.

### Hubs: GitHub, Hugging Face, Radicle

A git remote is a `hosted` medium — the copy survives deleting your working
tree, but someone else decides how long it stays. What makes it different
from a directory is what counts as being there:

```bash
./holdings.py add-medium github --kind other --durability hosted --site github
./holdings.py import-git github ~/projects/thing
```

It reads the tree of the **remote ref**, not your `HEAD`. Anything
uncommitted, unpushed or ignored is not on the hub, and recording it as
backed up would be wrong for exactly the files you are working on. You get
told when that gap exists:

```
WARNING: HEAD is 1 commit(s) ahead of origin/master. Anything only in those
commits is NOT on the remote and is not recorded here.
WARNING: 2 uncommitted or untracked path(s) in the working tree.
```

Placement is **exact**, and costs no network: the local object store already
holds the bytes, so `import-git` hashes them straight out of it. Measured on
this repo: 18 files, all by hash, 0.14s.

**git-lfs** pointers resolve to the content they point at — an LFS pointer
names its object by sha256, the same identity this catalog uses. That
matters most where it is used most: a Hugging Face model repo is pointers
almost all the way down, and hashing those would record a few hundred bytes
of text as if it were the weights.

One consequence worth expecting: if you have not `git lfs pull`ed, your
working tree holds the *pointer*, so scanning the checkout records the
pointer — which is correct, and means the model shows as unbacked on your
laptop while the hub has it. That is the true state of affairs.

**Radicle** works with the same command, being a git remote. But its
durability is not GitHub's: no custodian who can close your account, and in
exchange availability is the sum of whoever volunteers to seed you. The
quantity that ought to count is **seeds other than your own** — a repo
seeded only by your node is not a second copy — and holdings cannot see
that today, so it records one `hosted` copy, which is the honest floor
rather than the true number.

### Swarm as a medium

Content you have published to Swarm can count as a backup copy. It is a
`leased` medium — it survives deleting the original, and lapses when the
postage batch does — so the expiry can be read from the batch rather than
typed:

```bash
./holdings.py add-medium swarm --kind cloud --durability leased \
                               --lease-from-batch <batchID>
./holdings.py import-swarm swarm --root <bzz-root>
```

`import-swarm` records each file's **Swarm reference** next to the content
hash. The hash stays the identity; the reference is an address on one
medium, and storing it is what makes a remote copy checkable later without
downloading it again.

Which makes it the first remote medium that can be **checked** rather
than trusted:

```bash
./holdings.py check-swarm swarm --exit-code
# checked 24 reference(s) on 'swarm' (the head of each file):
#   24 retrievable, 0 not
```

An offsite drive has to be fetched and read. This asks the network instead,
and costs no data transfer worth the name. Measured against a live node:
24 references in **0.64s total**.

The default probes the head of each file — one ranged byte, which resolves
the reference and fetches the root chunk at the same cost whatever the file's
size (12 ms measured). `--deep` uses Bee's stewardship endpoint, which walks
every chunk: 1.0s for a 109-byte file, and no answer within 30s for a 138 MB
one. Thorough, and priced accordingly.

It confirms a copy is still *reachable at that address* — not that its bytes
hash to what the catalog recorded. So it updates the sighting and never the
verification date. A reference that does not answer is reported and **never
deleted**: a node that searched and found nothing is good evidence, not
proof, and quietly lowering a copy count is the direction this tool must not
err in.

The honesty about placement is the same as `import-restic`: a listing proves paths and
sizes, not bytes. Entries are matched to content already known by hash where
that is unambiguous, and recorded as `unverified:` where it is not. For
exact hashes, mount the root and `scan` it — reading the bytes is the only
thing that proves them. Either way, holdings only ever *reads* the manifest:
it does not upload, and stays out of the path your data takes to Swarm.

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

The full loop — encryption, feeds, postage renewal, and what to do when a
batch lapses — is in **[docs/PUBLISHING.md](docs/PUBLISHING.md)**.

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

Measured against a live Bee node on a 157 MB published catalog (120k files,
300k placements) — whole command, cold cache each time:

| command | pages fetched | of the file |
|---|---|---|
| `stats` | 4 | 0.01% |
| `media` | 4 | 0.01% |
| `whereis sha256:…` | 17 | 0.05% |
| `whereis <bare filename>` | 23 | 0.06% |
| `redundancy --min-copies 2` | 56 | 0.16% |
| `only-on laptop-x1` | 71 | 0.20% |

Page counts are stable; wall-clock varies with the network (0.9s to 90s for
the same query across runs), so pages are the honest measure.

`diff A B` is the exception and stays proportional to what is on A — it has
to ask "does B hold this too?" once per file, and no amount of precomputation
removes that. Fine locally, expensive over a network.

## The browser viewer (optional)

`web/` is a read-only viewer for a published catalog: media, `whereis`,
`redundancy` and `only-on` in a page with no server, no install and no
account. It is the same lazy-page trick as the CLI's Swarm reader, in
SQLite-WASM — so opening it does not download the catalog.

```bash
# The catalog follows a feed; the site is published once and stays put:
python web/publish.py --feed <owner-hex>/holdings
# Or freeze page, reader, wasm and catalog under one immutable root:
python web/publish.py --catalog ~/catalog.sqlite
```

Measured on the same 157 MB catalog, cold each time:

| in the page | pages fetched | of the file |
|---|---|---|
| opening it (media + totals) | 5 | 0.014% |
| `whereis` a bare filename | 20 | 0.056% |
| redundancy below 2 copies | 56 | 0.158% |
| only-on for one medium | 71 | 0.200% |

Identical to the CLI's counts, because it is the same SQL: `web/queries.json`
is generated from `holdings.QUERIES` and a test fails if the committed copy
disagrees. The viewer cannot quietly answer a different question from the
CLI — which matters, since every one of those report queries was rewritten
once already.

There is no write path in the page at all: `add-medium`, `scan` and
`import-restic` do not exist there, and the publisher does not ship
swarmlite's JS writer alongside the reader.

### Offline, on this machine

A published catalog is fetched page by page *at query time*, so it needs
connectivity and a gateway. `web/serve.py` answers the same page from here
instead — no Bee node, no wallet, no postage batch, and nothing installed
beyond the stdlib:

```bash
python web/serve.py --db ~/catalog.sqlite      # http://127.0.0.1:8765/
```

Which matters for a tool whose job is answering questions about drives you
are standing in front of with no signal.

It serves a **snapshot**, not the live file. SQLite's WAL sidecar is
invisible to the reader — measured: a write sitting in the WAL is simply
absent — so serving the live catalog would read it stale with no warning.
The snapshot is taken with sqlite3's own backup API, so it is consistent
even while a scan is writing, and the original is never touched. Re-run to
pick up a newer scan.

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
* Tests: `pip install -e ".[test]" && pytest` — **213 tests**, stdlib only, no
  node and no network; a guard fails if that number drifts from the suite.
* Roadmap: see [ROADMAP.md](ROADMAP.md) — v0.2 through v0.5, and which of the
  limits above are meant to change.
