# holdings User Guide

How to use it, in the order you will want it. Definitions live in the
[Reference](REFERENCE.md); publishing has its own [runbook](PUBLISHING.md).

The question holdings answers is **which media hold which files**, and the
two questions that follow from it: *is this backed up?* and *is it safe to
wipe this?*

---

## 1. Install and first catalog

```bash
pip install holdings
```

The catalog is one SQLite file. It defaults to
`~/.local/share/holdings/catalog.sqlite`; set `HOLDINGS_DB` or pass `--db`
to put it somewhere else.

Register the media you have, then scan each one while it is mounted:

```bash
holdings add-medium laptop-x1     --kind laptop --site home
holdings add-medium drive-budapest --kind drive --durability independent \
                                   --site budapest --location "safe, Budapest"

holdings scan laptop-x1 /home/you
holdings scan drive-budapest /media/you/backup-drive
```

Scanning is cheap to repeat: unchanged files are recognised by size and
mtime and not re-read.

Nothing here writes to your data. holdings only ever reads filesystems and
listings, and deleting the catalog costs nothing but convenience — every
fact in it can be rebuilt by scanning again.

## 2. Say what each medium survives

This is the one piece of judgement the tool needs from you, and it decides
every answer afterwards.

```bash
holdings add-medium dropbox --kind cloud --durability mirror
holdings add-medium github  --kind other --durability hosted --site github
holdings add-medium swarm   --kind cloud --durability leased --lease-expires 30d
```

| class | survives deleting the original? |
|---|---|
| `working` | no — it *is* the original |
| `independent` | yes, until it fails itself |
| `mirror` | **no — your deletion propagates** |
| `leased` | yes, until the lease lapses |
| `hosted` | yes, while someone else allows it |

The one that catches people is `mirror`. A Syncthing or Dropbox folder is
just a directory, so nothing stops you calling it a backup — but a file on
your laptop and in Dropbox has **two copies and no backup**, because one
`rm` removes both. Only `independent`, `leased` and `hosted` count.

`--backup` still works and means `--durability independent`.

## 3. Ask where something is

```bash
holdings whereis holiday.jpg          # by name
holdings whereis photos/holiday.jpg   # by path
holdings whereis sha256:9fe36f…       # by content
```

Content hash is identity, so two files with the same bytes are one thing
wherever they are and whatever they are called. A bare filename that matches
two *different* files is refused rather than guessed at.

## 4. Check a backup policy

```bash
holdings redundancy --min-copies 2
```

The whole of 3-2-1, not just the count:

```bash
holdings redundancy --min-copies 3 --min-sites 2 --min-kinds 2
```

- `--min-sites` is the *1 offsite*. Two drives in the same drawer as the
  laptop share fire, flood, theft and a ransomware process walking mounted
  volumes; counted as two, they are closer to one. Media with no `--site`
  recorded collapse into a single unknown site, because unknown is not the
  same as known-different.
- `--min-kinds` is the *2 media types*.

Add `--exit-code` and it becomes something cron can run:

```cron
0 9 * * *  holdings redundancy --min-copies 2 --exit-code || notify-send 'under-backed'
```

## 5. Before you wipe something

"Is everything on this laptop backed up?" is **not** what `only-on` answers.
`only-on` asks whether anything else holds the content, and a sync mirror
answers yes while being exactly the copy that does not survive.

Ask about backups instead, scoped to the medium you are about to destroy:

```bash
holdings redundancy --on laptop-x1 --min-copies 1 --exit-code || echo "not yet"
```

That counts only copies that survive deleting the original, and ignores
everything not on that medium — so an unbacked file on some other drive does
not block you.

For consolidating an old scattered drive, `only-on` is still the right list:
content that exists there and nowhere else at all.

```bash
holdings only-on drive-old --exit-code || echo "safe to wipe"
```

## 6. Seen is not verified

`seen_at` means the filesystem still listed the file at that size. It does
**not** mean anyone read it — a rescan reuses the stored hash without opening
the file, and bit rot changes neither size nor mtime. So a copy can be
faithfully catalogued for years and be gone.

Re-read a medium to actually verify it:

```bash
holdings scan drive-budapest /media/you/backup-drive --full
```

A `--full` scan that finds different bytes at a known path says so, instead
of quietly replacing the hash. On a medium nobody edits, that is what rot
looks like. A file that cannot be read is kept and reported, never pruned:
a failing drive and a tidied-up one must not produce the same change.

Then discount stale evidence, and plan the next re-read:

```bash
holdings redundancy --min-copies 2 --verified-within 365
holdings due
```

`due` orders media by what re-reading them would be worth — those holding
the only backup of something first, then the longest unread:

```
MEDIUM                 LAST READ       AGE   SOLE BACKUP FOR   TO RE-READ  WHERE
!drive-bp              2025-11-20     300d        2 (1.4 GB)       1.4 GB  budapest
 drive-home            2026-09-16       0d          1 (7 MB)         7 MB  home
```

## 7. Media you cannot mount

Anything that can list itself can be recorded. The same command reads them
all:

```bash
# restic
restic -r b2:bucket:repo ls --json latest | holdings import-restic restic-b2 -

# rclone — exact where the backend can produce SHA-256
rclone lsjson -R --hash remote:path | holdings import b2 - --format rclone

# a machine you can only reach over ssh, catalogued exactly
ssh nas 'cd /data && find . -type f -exec sha256sum {} +' \
    | holdings import nas - --format sha256sum

# anything else: {path, size, hash?, reference?} per line
holdings import mymedium listing.jsonl
```

The report separates three different claims, because they are worth
different amounts: **by hash** is exact, **matched by name+size** is a guess
that happened to be unique, and **unverified** is a placeholder that keeps
the file visible without pretending to know what it is.

A listing never proves bytes. Mounting the source and scanning it is the
only thing that does.

### Git remotes

```bash
holdings add-medium github --kind other --durability hosted --site github
holdings import-git github ~/projects/thing
```

Reads the tree of the **remote ref**, not your `HEAD` — anything
uncommitted, unpushed or ignored is not on the hub, and you are told when
that gap exists. Hashes come from the local object store, so it is exact and
costs no network. git-lfs pointers resolve to the content they name, which
matters on Hugging Face where a repo is pointers almost all the way down.

### Syncthing

```bash
holdings import-syncthing                        # which folders exist?
holdings add-medium nas --kind other --durability mirror --site attic
holdings import-syncthing nas --folder docs
```

Worth using over a plain scan because Syncthing knows what the *cluster*
holds and how complete each device is — facts about machines you cannot
scan. A device below 100% is reported rather than credited.

### Swarm

```bash
holdings add-medium swarm --kind cloud --durability leased \
                          --lease-from-batch <batchID>
holdings import-swarm swarm --root <bzz-root>
holdings check-swarm swarm --exit-code
```

`check-swarm` is the only remote medium you can check without fetching it:
it asks the network whether each copy is still retrievable. It confirms
reachability, not content, so it never claims verification — and a missing
reference is reported, never deleted.

### Radicle

```bash
holdings add-medium radicle --kind other --durability hosted
holdings import-git  radicle ~/projects/thing
holdings rad-seeds   radicle --rid rad:z3gqcJ…
```

Radicle has no custodian who can close your account, and in exchange
availability is the sum of voluntary seeds — so what counts is **seeds other
than your own**. `rad-seeds` counts them from the node's routing table. A
repo seeded only by your node is your node, and stops counting as a backup.

## 8. Categories, joined to placement

holdings answers *where*; OntoDAG answers *what it is about*. The join is the
reason both exist — *"which Vienna photos are unbacked?"* — and it happens by
composing the memberships where both halves are present, then materialising
them:

```bash
# on the machine that has both: ask OntoDAG for composed memberships
odag get vienna --items-only | ... > cats.jsonl    # {"item": "sha256:…", "categories": [...]}

holdings import-categories cats.jsonl --source-key "$(odag inspect --root)"
holdings redundancy --category vienna --min-copies 1
```

holdings does not compute categories and never will. It reads a listing, the
way it reads every other source, and the import is a full rebuild rather than
a merge — staleness is permitted here, drift is not. Content the catalog has
never seen is skipped and counted rather than recorded.

`--source-key` records what the memberships were composed from, so a reader
can tell whether they still describe this catalog. That is the condition
ontodag's projection contract gained when it stopped requiring derived data
to stay on the machine that built it.

Cost is proportional to the category, not the catalog: measured at 40 rows in
10 ms over a 40,000-object catalog. This is a prototype — see
[ontodag#20](https://github.com/petfold/ontodag/issues/20).

## 9. Scripting it

Every read command takes `--json` and prints one object:

```bash
holdings stats --json | jq .content
holdings only-on drive-old --json | jq -r '.items[].path'
holdings due --json | jq -r '.media[] | select(.overdue) | .medium_id'
```

`redundancy`, `only-on`, `due` and `check-swarm` take `--exit-code`, which is
what makes them usable from a timer.

## 10. Reading the catalog somewhere else

The catalog is a path, so how it travels is not holdings' business. Put it in
a synced folder and every device carries it. Or publish it read-only and
query it without holding it:

```bash
swarmlite publish ~/catalog.sqlite --encrypt --feed holdings --signer "$KEY"
holdings --db bzzf://<owner>/holdings/catalog.sqlite whereis holiday.jpg
```

A lookup fetches tens of kilobytes of a catalog that may be hundreds of
megabytes. The full loop — encryption, feeds, postage renewal — is in
[PUBLISHING.md](PUBLISHING.md).

From a checkout there is also a browser viewer, published or served locally:

```bash
python web/serve.py --db ~/catalog.sqlite      # offline, no node, no wallet
python web/publish.py --feed <owner-hex>/holdings
```

## 11. Keeping it honest

```bash
holdings check
```

Derived columns are a cache over the base tables. `check` recomputes them and
reports any disagreement, along with leases and replica counts that cannot be
relied on. Staleness is a permitted failure mode here; drift is not.

If anything is ever wrong, the remedy is the same as the design: scan again.
The only irreplaceable data in the system is your own categorisation, which
holdings never touches.
