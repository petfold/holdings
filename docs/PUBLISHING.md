# Publishing a catalog

How to put a catalog somewhere other devices can query it, and how to keep
it there. The tutorial is the [User Guide](USER_GUIDE.md); definitions are
in the [Reference](REFERENCE.md). Entirely optional: a local catalog, or one in a synced folder, is
the default and needs none of this.

The point is reach without uptime overlap. A synced folder needs the writing
device online at the same time as the reader; a published catalog is a
snapshot the network holds, so a phone can answer `whereis` against a laptop
that has been shut for a week.

## Before you start

```bash
pip install 'holdings[swarm]'      # the reader; publishing uses swarmlite's CLI
```

You need a Bee node and a postage batch. Reading needs neither — only
whoever *serves* the content does.

## The loop

Scan as usual, then publish. Publishing is a separate tool working on the
same file; holdings never writes to the network itself.

```bash
holdings --db ~/catalog.sqlite scan drive-budapest /media/you/backup-drive
swarmlite publish ~/catalog.sqlite --encrypt --feed holdings --signer "$KEY"
# pin:  bzz://<root>/catalog.sqlite      <- this exact version, forever
# feed: bzzf://<owner>/holdings/catalog.sqlite  <- always the latest
```

Then, from anywhere:

```bash
holdings --db bzzf://<owner>/holdings/catalog.sqlite whereis holiday.jpg
```

Publish **after every scan**, or readers are looking at old placement. There
is no way for them to tell that a scan happened and was not published — the
one thing a reader cannot detect — so it is on the writer to keep the habit.
What holdings does warn about is the underlying facts being old: if nothing
in a published catalog has been scanned for 30 days, reads print a warning
(`--max-scan-age DAYS`, or `0` to silence).

## `--encrypt` is not optional in practice

A catalog carries filenames, directory structure, file sizes and your
`--location` hints — "safe, Budapest flat" — and Swarm is a public network.
Encrypt it. The root becomes 128 hex characters and **the URL is the
secret**: anyone with it can read the catalog, readers need no flag, and
feeds carry the full reference.

Two consequences worth being clear-eyed about:

- You cannot un-publish. Every version is a permanent pin, so a leaked root
  is leaked retroactively, for every snapshot published under it.
- Losing the signer key means losing the feed, though old pins keep working.

## Feed or pin

Publish into a feed **from the first version** if the data will ever change,
which for a catalog it will. The same upload advances the feed and yields a
pin, so it costs nothing extra, and readers get one stable URL from day one.
`swarmlite snapshots "bzzf://<owner>/holdings/catalog.sqlite"` lists the
whole history; every line is still queryable.

Use a bare `bzz://` pin when you want a specific version to stay answerable
— before wiping a drive, say.

## Erasure coding: what survives chunk loss

Uploads use **redundancy level 2** by default — erasure coding, so the
published root still resolves when some of its chunks cannot be retrieved.
`web/publish.py` passes it explicitly rather than inheriting it, and
`swarmlite publish` gets the same level from swarmfs's default.

It is worth knowing the level exists, because it is the setting that decides
whether a published catalog is readable after partial loss, and because it
costs more stamped chunks — `--buy` sizing already accounts for that. Levels
run 0–4; `web/publish.py --redundancy N` changes it.

This is a property of the *upload*, not of the postage. A batch running out
still takes everything with it, whatever the erasure level.

## Stamps, and the renewal that is now your job

A published root lives exactly as long as its postage batch, and **an
expired batch cannot be revived**.

```bash
swarmlite stamps                            # life left, bucket headroom
swarmlite stamps --check --min-ttl 7d       # exit 1 when renewal is due
swarmlite stamps topup <batchID> --for 4w   # extend it
```

Put the check on a timer — it is cron-shaped for a reason:

```cron
0 9 * * 1  swarmlite stamps --check --min-ttl 14d || notify-send 'holdings: postage expiring'
```

Note that the TTL a node reports is an *estimate* at the current storage
price. If the price rises, the batch drains faster and expiry arrives sooner
than the figure you were quoted. Treat it as an optimistic bound and renew
with margin.

### When a batch lapses anyway

Nothing is lost. The local catalog is authoritative and the catalog is
regenerable by re-scanning, so recovery is: buy a batch, republish, carry
on. This is the one dataset in the system where expiry is survivable, and it
is survivable by design rather than by luck.

That property does **not** transfer to anything whose only copy is on Swarm.
If you ever publish something irreplaceable — the human OntoDAG
categorisation, a vault — the invariant to hold is that Swarm is never the
sole copy.

## A page, not just a CLI

`web/publish.py` publishes a read-only viewer — media, `whereis`,
`redundancy`, `only-on` — that anyone can open in a browser with nothing
installed. Pair it with a feed so the site is published once and only the
catalog is republished after each scan:

```bash
python web/publish.py --feed <owner-hex>/holdings
```

See the README's *browser viewer* section for what it costs to read.

## What publishing does not give you

- **Not a write path.** Published catalogs are read-only. Every write
  command — `add-medium`, `scan`, and all the importers — refuses a URL and
  points back at the local file. Placement is written where the scanning
  happens.
- **Not offline reads.** A reader fetches pages over the network at query
  time. A synced file works on a plane; a published one does not.
- **Not a backup of the catalog.** Readers hold no copy — a `whereis`
  fetches tens of pages, not the file. Under a synced folder every peer was
  an accidental full replica; under publishing, none are. Back up the
  machine that holds the local catalog.

## Costs, measured

On a 157 MB catalog (120k files, 300k placements) against a live node:
publishing took about 2.5 minutes; reads fetched 4–71 pages per command
(0.01%–0.20% of the file). `swarmlite publish` runs `VACUUM` each time, so
how much of a republish deduplicates against the previous one depends on
page layout staying stable — worth watching on your own data if you publish
often.
