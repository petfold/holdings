# holdings Reference

Definition-first, no narrative. The tutorial is the
[User Guide](USER_GUIDE.md); publishing has its own
[runbook](PUBLISHING.md); the plan is [ROADMAP.md](../ROADMAP.md).

**Every command, flag and enumeration below is pinned against the code by
`tests/test_reference.py`.** If this file and the CLI disagree, the suite
fails. Column lists and defaults are not pinned — check them against
`holdings.py` if a detail matters.

Describes version `0.2.1`.

---

## 1. Vocabulary

| term | definition |
|---|---|
| medium | Somewhere content lives: a drive, a laptop, a restic repo, a Swarm root, a git remote. Registered with `add-medium`. |
| content | Bytes, identified by `sha256:…`. The primary key everywhere. |
| instance | One content object at one path on one medium. |
| durability | What a medium's copies survive — see §5. `is_backup` is derived from it, never asserted. |
| site | Where a medium physically is. Media sharing a site are assumed to fail together. |
| seen | The medium listed the file. Says nothing about its bytes. |
| verified | The bytes were read and hashed to the value recorded. |
| evidence | How an instance is known: `hashed`, `metadata`, `retrievable`, `imported`. |
| lease | A copy that lapses on a schedule unless renewed (postage). |
| replicas | Independent hosts holding a `hosted` medium, **not** counting your own machine. |

## 2. Install

| command | gives |
|---|---|
| `pip install holdings` | the CLI. Stdlib only, single module, no dependencies. |
| `pip install 'holdings[swarm]'` | plus reading a catalog published to Swarm by URL. |
| `pip install -e '.[test]'` | plus pytest, for the suite. |

`web/publish.py` and `web/serve.py` are **not** in the wheel; they need a
checkout, and swarmlite's JavaScript.

## 3. Global flags

| flag | default | meaning |
|---|---|---|
| `--db` | `$HOLDINGS_DB`, else `~/.local/share/holdings/catalog.sqlite` | the catalog: a path, or a `bzz://` / `bzzf://` / `file://` / `memory://` URL (needs `holdings[swarm]`) |
| `--max-scan-age` | 30 | warn when a *published* catalog's newest scan is older than this many days; `0` silences |

Every read command also takes `--json`, which prints one JSON object.

## 4. Commands

Write commands refuse a published catalog: placement is written where the
scanning happens.

### `holdings add-medium <medium_id>` — writes

Register or update a medium.

| flag | meaning |
|---|---|
| `--kind` | `drive`, `laptop`, `phone`, `restic-repo`, `cloud`, `other` |
| `--durability` | see §5; defaults to `independent` with `--backup`, else `working` |
| `--backup` | shorthand for `--durability independent` |
| `--site` | a name you reuse (`home`, `budapest`); media sharing one fail together |
| `--location` | free text for humans |
| `--lease-expires` | for `leased`: `YYYY-MM-DD`, or `30d` / `4w` from now |
| `--lease-from-batch` | read the expiry from a Swarm postage batch (needs the extra) |
| `--replicas` | for `hosted`: independent hosts, not counting your own machine |
| `--notes` | free text |
| `--api-url` | Bee API, for `--lease-from-batch` |

Re-registering keeps the site, lease and replica count unless restated.

### `holdings scan <medium_id> <mount_path>` — writes

Walk a mounted medium and record what is there.

| flag | meaning |
|---|---|
| `--root` | record paths relative to this subtree |
| `--exclude-file` | extra exclude patterns, one per line |
| `--full` | rehash everything, ignoring the size+mtime shortcut |

Unchanged files are recognised by size+mtime and **not re-read**, so a plain
rescan sets `evidence=metadata` and does not advance `verified_at`. `--full`
re-reads, sets `evidence=hashed`, and reports any path whose bytes now hash
differently. Symlinks are skipped. Unreadable files are kept and reported,
never pruned.

### `holdings import <medium_id> [listing]` — writes

Record any listing of paths and sizes. `-` reads stdin.

| flag | meaning |
|---|---|
| `--format` | `jsonl` (default), `restic`, `rclone`, `sha256sum` — see §7 |

### `holdings import-restic <medium_id> [listing]` — writes

`import --format restic`, kept as a name scripts already use.

### `holdings import-swarm <medium_id> [listing]` — writes

| flag | meaning |
|---|---|
| `--root` | list a published root from a node instead of reading a file |
| `--api-url` | Bee API |

Carries each file's Swarm reference into `instances.external_ref`.

### `holdings import-git <medium_id> <repo>` — writes

Record what a git remote holds: GitHub, Hugging Face, Radicle.

| flag | meaning |
|---|---|
| `--ref` | the remote ref to read (default: this branch's upstream) |
| `--remote` | default `origin` |

Reads the tree of the **remote ref**, not `HEAD`. Hashes come from the local
object store, so placement is exact and costs no network. git-lfs pointers
resolve to the content they name. Warns when `HEAD` is ahead, or the working
tree is dirty.

### `holdings import-syncthing [medium_id]` — writes

Record what a Syncthing folder holds. With no `--folder`, lists folders.

| flag | meaning |
|---|---|
| `--folder` | folder id |
| `--device` | which device this medium is (default: the local one) |
| `--api-url` | default `http://127.0.0.1:8384` |
| `--api-key` | else `$SYNCTHING_API_KEY`, else Syncthing's `config.xml` |
| `--home` | Syncthing's config directory |

`db/browse` is the **cluster's** index. A device below 100% is reported, not
credited.

### `holdings check-swarm <medium_id>` — writes

Ask the network whether a medium's copies are still retrievable. Needs no
optional extra — urllib and a reachable node.

| flag | meaning |
|---|---|
| `--deep` | walk every chunk (stewardship) instead of probing each file's head |
| `--workers` | parallel probes, default 8 |
| `--timeout` | seconds per probe, default 30 |
| `--limit` | check at most this many; 0 = all |
| `--exit-code` | exit 1 when anything is not retrievable |
| `--json` | |
| `--api-url` | Bee API |

Confirms reachability at an address, not content: updates `seen_at`, never
`verified_at`. A missing reference is reported and **never deleted**.

### `holdings rad-seeds <medium_id>` — writes

Count who else seeds a Radicle repo, and record it as `replicas`.

| flag | meaning |
|---|---|
| `--rid` | repo id (default: the repo you are in) |
| `--home` | `RAD_HOME`, if not the default |
| `--json` | |

Counts distinct nids from `rad node routing --json` minus this node's own.
**Refuses** when the node is stopped: an empty routing table is not evidence
of zero seeds.

### `holdings import-categories [listing]` — writes

Materialise OntoDAG categories against placement, so a reader can join the
two without a lattice. **Prototype**, and opt-in: a catalog only has
categories if someone imported them.

| flag | meaning |
|---|---|
| `--source-key` | what the memberships were composed from, so staleness is detectable |

Reads JSON lines of `{"item": "<hash>", "categories": [...]}`, which `odag`
produces from its composed view. Full rebuild, not diffing. Categories for
content this catalog has never seen are skipped and counted, never recorded.

holdings does not compute categories: semantics belong to OntoDAG. See
[ontodag PROJECTIONS.md §3](https://github.com/petfold/ontodag/blob/main/docs/plans/PROJECTIONS.md),
amended 2026-09-17 to permit exactly this.

### `holdings whereis <target>` — read-only

Which media hold this content. `<target>` is a path, a bare filename, or a
`sha256:…` hash. An ambiguous filename is refused rather than guessed.

### `holdings redundancy` — read-only

Content below a backup policy.

| flag | default | meaning |
|---|---|---|
| `--min-copies` | 2 | backup copies required |
| `--on` | — | only content present on this medium — the question before wiping it |
| `--category` | — | only content in this OntoDAG category (needs `import-categories`) |
| `--min-sites` | 1 | the *1 offsite* of 3-2-1 |
| `--min-kinds` | 1 | the *2 media types* of 3-2-1 |
| `--verified-within` | 0 | require some backup copy to have been read within N days |
| `--lease-margin` | 14 | a lease with less than this left is at risk |
| `--replica-age` | 30 | a replica count older than this is at risk |
| `--limit` | 40 | rows shown |
| `--exit-code` | — | exit 1 on any violation, including a lease or replica at risk |

Only `--min-copies` uses the index; the other thresholds cost a scan.

### `holdings only-on <medium_id>` — read-only

Content that exists on this medium and **nowhere else at all**. Note this is
not the wipe question — a sync mirror counts as elsewhere. Use
`redundancy --on` for that.

| flag | default | meaning |
|---|---|---|
| `--limit` | 40 | |
| `--exit-code` | — | exit 1 while anything still exists only here |

### `holdings diff <medium_a> <medium_b>` — read-only

On A but not on B. Proportional to what is on A.

| flag | default |
|---|---|
| `--limit` | 40 |

### `holdings due` — read-only

Which media are overdue for re-reading, most urgent first: media holding the
only backup of something, then the longest unread.

| flag | default | meaning |
|---|---|---|
| `--stale-after` | 180 | days before a medium counts as overdue |
| `--exit-code` | — | exit 1 when any medium is overdue |

### `holdings media` — read-only

Every medium, with durability, site, totals, last scan and last read.

### `holdings stats` — read-only

Catalog totals.

### `holdings check` — read-only

Verify derived columns against the base tables, and report leases and
replica counts that cannot be relied on. Exits 1 on drift.

### `holdings project-ontodag` — read-only

Emit the `sys:` placement projection as JSON lines.

| flag | default |
|---|---|
| `--out` | `-` (stdout) |

## 5. Durability classes

What a medium's copies survive, and whether they count as a backup.

| class | survives deleting the original? | counts | examples |
|---|---|---|---|
| `working` | no — it *is* the original | no | the laptop you edit on |
| `independent` | yes, until it fails itself | yes | second drive, restic repo |
| `mirror` | **no — your deletion propagates** | no | Syncthing, Dropbox, Drive |
| `leased` | yes, until the lease lapses | yes | Swarm postage |
| `hosted` | yes, while someone else allows it | yes* | GitHub, Hugging Face, Radicle |

\* `hosted` with `replicas = 0` does **not** count: hosting nobody else
participates in is your own machine. `replicas` unset means the question
does not arise (one custodian) and is never read as zero.

holdings cannot verify a class. It is your assertion when you register a
medium.

## 6. Evidence

How an instance is known, weakest first.

| value | means |
|---|---|
| `imported` | a listing said so — paths and sizes, never bytes |
| `metadata` | the filesystem listed it at the same size and mtime |
| `retrievable` | the network says a copy can still be fetched at that address |
| `hashed` | the bytes were read and hashed to the recorded value |

Only `hashed` sets `verified_at`.

## 7. Listing formats

| format | source | identity |
|---|---|---|
| `jsonl` | `{path, size, hash?, reference?}` per line | exact when `hash` given |
| `restic` | `restic ls --json <snapshot>` | name+size |
| `rclone` | `rclone lsjson -R [--hash] remote:path` | exact where the backend supplies SHA-256 |
| `sha256sum` | `sha256sum` output | exact, but no sizes — an unknown hash is skipped, never invented |

Identity is resolved in that order of worth: a supplied sha256 is taken as
given; otherwise name+size is matched against known content when
unambiguous; otherwise the entry is recorded under an `unverified:`
placeholder carrying the medium's name, so the same unidentified file on two
media stays two objects.

## 8. Schema

Placement truth. Semantics belong to OntoDAG.

| table | holds |
|---|---|
| `media` | registered media, their durability, site, lease, replicas, and derived totals |
| `content` | one row per sha256, with derived copy counts and `example_path` |
| `instances` | one row per (medium, path): hash, size, mtime, `seen_at`, `verified_at`, `evidence`, `external_ref` |
| `scans` | one row per scan |
| `catalog_summary` | one row of totals, so `stats` reads no tables |
| `backup_histogram` | counts by `backup_copies`, so `redundancy`'s total is a few rows |
| `categories` | human categories composed by OntoDAG, materialised against content — optional |
| `category_source` | what those memberships were composed from, and when |

Derived columns are a cache over the base tables, recomputed wholesale after
every write command. `holdings check` reports any disagreement. They are
never authoritative: re-scanning rebuilds everything.

## 9. Exit codes

| code | when |
|---|---|
| 0 | success |
| 1 | a policy violation with `--exit-code`, drift from `check`, or an error |

`--exit-code` is available on `redundancy`, `only-on`, `due` and
`check-swarm`, which is what makes them usable from cron.

## 10. Environment

| variable | used by |
|---|---|
| `HOLDINGS_DB` | default catalog path |
| `SYNCTHING_API_KEY` | `import-syncthing` |
| `BEE_API_URL` | default Bee node for `check-swarm` |
| `CI` | makes the README test-count guard enforce rather than skip |
