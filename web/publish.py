"""Assemble and publish the read-only viewer as one Swarm site.

    python web/publish.py --feed <owner-hex>/holdings       # follows a feed
    python web/publish.py --catalog ~/catalog.sqlite        # one frozen root

Needs the optional extra (`pip install 'holdings[swarm]'`) and a Bee node
with a usable postage batch. Nothing here is part of the holdings module —
the CLI stays stdlib-only.

Two shapes, and the choice matters operationally:

  --feed    The page resolves the feed at load, so the catalog is published
            separately (and republished after every scan) while this site
            stays put. What you want for ongoing use.
  --catalog A prepared copy of the catalog is published *inside* the site,
            so page, reader, wasm engine and data share one immutable root
            and nothing can drift apart. A snapshot: updating it means
            republishing the whole site, and readers need the new root.

Layout under the published root, everything relative:

    index.html  app.js  queries.json  config.json  [catalog.sqlite]
    swarmlite/{index.js, SwarmVFS.js, mantaray.js, verify.js, feeds.js}
    vendor/  (wa-sqlite, js-sha3, noble-secp256k1 — copied whole)
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"

# Only what the *reader* needs. publisher.js and prepare.js are deliberately
# left out: this site has no write path, and not shipping one is a clearer
# guarantee than not calling it.
READER_SRC = ["index.js", "SwarmVFS.js", "mantaray.js", "verify.js",
              "feeds.js"]


def find_swarmlite_js(explicit: str | None) -> Path:
    """Locate swarmlite's js/ tree (its npm package is not a Python dep)."""
    candidates = [Path(explicit)] if explicit else [
        REPO.parent / "swarmlite" / "js",
        REPO / "node_modules" / "swarmlite",
    ]
    for c in candidates:
        if (c / "src" / "index.js").is_file():
            return c
    sys.exit("cannot find swarmlite's js/ tree. Pass --swarmlite-js PATH, or"
             " clone https://github.com/petfold/swarmlite next to this repo,"
             " or `npm install swarmlite` here.")


# `[^;]` and DOTALL on purpose: a multi-line `import { a, b } from './x.js'`
# is the commonest shape there is, and an earlier newline-excluding version
# of this silently matched none of them -- so the check passed while the
# assembled site was missing a module.
IMPORT_RE = re.compile(r"""(?:^|\s)(?:import|export)\b[^;]*?"""
                       r"""\bfrom\s+['"](\.[^'"]+)['"]""", re.M | re.S)


def strip_comments(js: str) -> str:
    """Block and whole-line comments only.

    Example imports live in comments (swarmlite's own index.js documents its
    usage that way), and counting those as real ones makes this check cry
    wolf. Trailing `//` comments are left alone: stripping them would need
    to know about string literals, and `http://` is everywhere.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"^\s*//.*$", "", js, flags=re.M)


def check_imports(site: Path) -> None:
    """Fail here rather than in someone's browser.

    Every relative import in the assembled JS must resolve to a file that
    was actually copied. A missing one is invisible until the page loads,
    by which point the site has a permanent root.
    """
    missing = []
    for f in sorted([*site.rglob("*.js"), *site.rglob("*.mjs")]):
        for target in IMPORT_RE.findall(strip_comments(f.read_text(errors="ignore"))):
            if not (f.parent / target).resolve().is_file():
                missing.append(f"{f.relative_to(site)} -> {target}")
    if missing:
        sys.exit("assembled site has unresolved imports:\n  "
                 + "\n  ".join(sorted(missing)))


def copy_page(site: Path, js: Path) -> None:
    """Put the page, the reader and its vendored engine into `site`.

    Shared with serve.py: publishing and serving locally differ only in
    where the catalog comes from and who answers the range requests.
    """
    site.mkdir(parents=True, exist_ok=True)
    shutil.copy(WEB / "index.html", site / "index.html")
    for name in ("app.js", "view.js"):
        shutil.copy(WEB / name, site / name)

    # Regenerated rather than copied, so a site can never be served or
    # published with SQL the CLI has moved on from. (A test guards the
    # committed copy too.)
    sys.path.insert(0, str(REPO))
    import holdings
    (site / "queries.json").write_text(
        json.dumps(holdings.QUERIES, indent=2) + "\n")

    (site / "swarmlite").mkdir(exist_ok=True)
    for name in READER_SRC:
        shutil.copy(js / "src" / name, site / "swarmlite" / name)
    # Copied whole, not hand-picked. Curating this list by reading imports
    # is how the first attempt shipped a site missing noble-secp256k1 --
    # which verify.js needs and nothing noticed until the page ran.
    if not (site / "vendor").exists():
        shutil.copytree(js / "vendor", site / "vendor")


def write_config(site: Path, config: dict) -> None:
    (site / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    check_imports(site)


def assemble(site: Path, js: Path, args) -> None:
    copy_page(site, js)
    config = {
        "name": args.name,
        "label": args.label,
        "verify": args.verify,
        "maxScanAgeDays": args.max_scan_age,
    }
    if args.feed:
        config["feed"] = args.feed
        if args.api_url:
            config["api"] = args.api_url
    if args.catalog:
        from swarmlite.publish import prepare
        warnings = prepare(args.catalog, str(site / args.name))
        for w in warnings:
            print(f"note: {w}")
    write_config(site, config)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--feed", metavar="OWNER/TOPIC",
                     help="page resolves this feed at load")
    src.add_argument("--catalog", metavar="PATH",
                     help="publish a prepared copy inside the site")
    ap.add_argument("--name", default="catalog.sqlite",
                    help="the catalog's filename (default catalog.sqlite)")
    ap.add_argument("--label", default="published catalog",
                    help="shown in the page header")
    ap.add_argument("--api-url", dest="api_url")
    ap.add_argument("--stamp", default="auto")
    ap.add_argument("--redundancy", type=int, default=2, choices=range(5),
                    metavar="0-4",
                    help="erasure-coding level for the upload (default 2):"
                         " how much chunk loss the published root survives")
    ap.add_argument("--verify", action="store_true",
                    help="check every chunk against the root client-side"
                         " (for an untrusted gateway)")
    ap.add_argument("--max-scan-age", type=float, default=30.0, metavar="DAYS")
    ap.add_argument("--swarmlite-js", help="path to swarmlite's js/ directory")
    ap.add_argument("--out", help="assemble here and do not publish")
    args = ap.parse_args()

    js = find_swarmlite_js(args.swarmlite_js)

    if args.out:
        assemble(Path(args.out), js, args)
        print(f"assembled at {args.out} (not published)")
        return 0

    import fsspec
    # Erasure level 2, stated rather than inherited. swarmfs defaults to it,
    # but a silently inherited durability setting is the kind that changes
    # under you -- and this is the setting that decides whether the site
    # survives chunk loss. It costs more stamped chunks, which `--buy`
    # sizing already accounts for.
    opts = {"stamp": args.stamp, "redundancy": args.redundancy}
    if args.api_url:
        opts["api_url"] = args.api_url
    fs = fsspec.filesystem("bzz", **opts)

    with tempfile.TemporaryDirectory() as d:
        site = Path(d) / "site"
        assemble(site, js, args)
        total = sum(f.stat().st_size for f in site.rglob("*") if f.is_file())
        print(f"uploading {total / 2 ** 20:.1f} MB site ...")
        root = fs.upload(str(site))

    api = args.api_url or "http://localhost:1633"
    print(f"root: {root}")
    print(f"site: {api}/bzz/{root}/index.html")
    if args.feed:
        print("     (the catalog is published separately — republish it after"
              " each scan; this site does not need republishing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
