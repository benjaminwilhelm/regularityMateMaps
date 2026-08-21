#!/usr/bin/env python3
"""
Repack a ``maps-v2`` region zip into the ``maps-v3`` corridor format.

Why this exists
---------------

``maps-v2`` ships one zip per country. Driving a 46 km route in Switzerland
therefore costs 227 MB, and in Germany 1.76 GB, which in practice means Germany
does not work at all. Measured on a real device, the tiles a 15 km corridor
around that Swiss route actually needs come to **30 MB -- 5% of the country**.

``maps-v3`` makes that slice fetchable without changing a single byte of tile
content: this script is a mechanical repack, not a rebuild. The tiles it emits
are identical to the ones in ``maps-v2``.

The format
----------

Two assets per region::

    <region>.vtar       POSIX tar, uncompressed container, whose members are
                        individually gzipped tiles:  2/000/786/992.gph.gz
    <region>.idx.json   the only way in

Three decisions in that sentence are load-bearing:

* **A real tar rather than a bare concatenation.** The whole-country download
  path stays a stream-and-extract exactly as the zip is today, and a region can
  be inspected by hand with ``tar`` and no bespoke tooling.

* **Members compressed individually, not the container.** A compressed container
  cannot be sliced -- that is precisely why the v2 zip cannot serve a corridor
  cheaply. Per-member compression keeps the whole-country download near today's
  size while leaving every tile independently fetchable by byte range. The cost
  is a slightly worse ratio than one big stream, because there is no shared
  dictionary across tiles; that is the price of random access.

* **gzip rather than zstd.** ``java.util.zip`` and Foundation both ship gzip, so
  the client gains no native dependency. zstd would compress binary graph tiles
  somewhat better and cost a new third-party dependency on two platforms.

The index carries a sha256 per tile. A ranged fetch that is interrupted and
resumed badly yields a corrupt tile, and a corrupt Valhalla tile is *silent
wrong routing* rather than a clean failure -- cheap insurance at ~64 bytes each.

Slicing a cluster
-----------------

One country per asset is not a design, it is a limit: Valhalla tiles are not
independent. A directed edge stores its end node as a GraphId carrying the
neighbour tile's node *index*, and that index is handed out while that tile is
built (``baldr/directededge.h``: ``endnode_ : 46``, ``opp_index_ : 7``). France
built alone and Italy built alone therefore number the cell they share
differently, and a corridor drawn from both routes onto whatever node now sits
at that index -- wrong roads, and no error anywhere.

So a *cluster* is one ``valhalla_build_tiles`` run over several merged extracts,
and this script slices the single tree it produces into release assets under
GitHub's 2 GiB ceiling::

    python3 repack_v3.py --tiles ./out/tiles --cluster eu_alps --out ./packed

Slicing is packaging and nothing more -- every asset descends from one build, so
every border inside the cluster joins up. Assets are filled in key order, which
makes each one a contiguous span of tile ids, so the manifest can say which
asset holds a cell in a few bytes per asset instead of a map of every key. The
levels are ordered 0, 1, 2 deliberately: levels 0 and 1 are the whole cluster's
long-distance graph and only a few hundred tiles, so they land together in the
first asset and every corridor fetches that one index plus one or two others.

Usage
-----

    python3 repack_v3.py --zip ch_full.vtiles.zip --region ch_full --out ./out
    python3 repack_v3.py --all --out ./out          # every region in regions.json
    python3 repack_v3.py --tiles ./tiles --cluster eu_alps --out ./packed
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Bumping this invalidates every client's tile pool, because Valhalla edges
# reference their neighbours by tile id and a tile from one build sitting beside
# a neighbour from another is wrong routing rather than an error. Bump rarely --
# quarterly at most, and never for a cosmetic change.
BUILD_ID = "v3-2026-08"

MAPS_V2_BASE = "https://github.com/benjaminwilhelm/regularityMateMaps/releases/download/maps-v2/"

# ``<level>/<3-digit>/.../<3-digit>.gph`` anywhere under the archive root. The
# number of path components varies by level (see level_of below), so this
# deliberately does not pin a component count.
TILE_RE = re.compile(r"(?:^|/)(\d)/((?:\d{3}/)*\d{3})\.gph$")


def tile_key(path: str) -> str | None:
    """``…/2/000/786/992.gph`` -> ``"2/786992"``, or None if not a tile."""
    m = TILE_RE.search(path)
    if not m:
        return None
    level = int(m.group(1))
    tile_id = int(m.group(2).replace("/", ""))
    return f"{level}/{tile_id}"


def member_name(key: str) -> str:
    """``"2/786992"`` -> ``2/000/786/992.gph.gz``, Valhalla's own layout.

    The digit width differs per level -- six for levels 0 and 1, nine for level
    2 -- because Valhalla pads the id to cover the largest id that level can
    hold. Assuming nine everywhere produces paths that do not exist, silently,
    and cost an afternoon the first time.
    """
    level_s, id_s = key.split("/")
    level, tile_id = int(level_s), int(id_s)
    width = 9 if level == 2 else 6
    digits = f"{tile_id:0{width}d}"
    groups = [digits[i:i + 3] for i in range(0, len(digits), 3)]
    return f"{level}/" + "/".join(groups) + ".gph.gz"


# GitHub rejects a release asset at 2 GiB, and reports it as an opaque HTTP 422
# after the upload rather than before it. Slicing stops short of the ceiling so
# a tar's own padding cannot carry an asset over it.
MAX_ASSET_BYTES = 1_900_000_000


def level_of(key: str) -> int:
    return int(key.split("/")[0])


def id_of(key: str) -> int:
    return int(key.split("/")[1])


def sort_key(key: str) -> tuple[int, int]:
    """Level first, then id.

    Levels 0 and 1 are the cluster's long-distance graph and only a few hundred
    tiles; putting them first lands them together in the opening asset, so every
    corridor reads that index plus one or two others rather than all of them.
    Within a level, ids ascend, and a level-2 id is ``row * 1440 + col`` -- so
    ascending order walks the world in latitude bands and a corridor's tiles
    stay close together in the sequence.
    """
    return (level_of(key), id_of(key))


def gzip_blob(raw: bytes, gzip_level: int) -> bytes:
    # mtime=0 keeps the gzip header deterministic; without it the same input
    # produces a different archive on every run.
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=gzip_level, mtime=0) as gz:
        gz.write(raw)
    return buf.getvalue()


def add_tile(tf: tarfile.TarFile, key: str, blob: bytes, raw: bytes) -> dict:
    """Writes one tile into an open tar and returns its index entry."""
    ti = tarfile.TarInfo(member_name(key))
    ti.size = len(blob)
    ti.mtime = 0
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    tf.addfile(ti, io.BytesIO(blob))

    # tar writes a 512-byte header immediately before the payload, so the
    # payload begins at the stream position after addfile minus its own padded
    # length. Recording the *payload* offset is what lets a client range-GET the
    # bytes alone and skip tar parsing entirely.
    end = tf.fileobj.tell()
    padded = (len(blob) + 511) // 512 * 512
    return {
        "o": end - padded,
        "c": len(blob),
        "u": len(raw),
        "h": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


LEVEL_DEGREES = {0: 4.0, 1: 1.0, 2: 0.25}

# A Valhalla tile with no edges in it is a few hundred bytes of header. The
# alpine build has 126 of them, scattered across the Mediterranean, because
# `osmium extract --strategy complete_ways` keeps whole ways that touch the box
# and some of those ways -- ferry routes, administrative boundaries -- run for
# hundreds of kilometres. They are harmless to publish and cost nothing, but
# they must not define where the cluster claims to be: unfiltered they put the
# western Alps' footprint at Gibraltar. The distribution is sharply bimodal
# (median 392 kB against a 312-byte floor), so any threshold in between
# separates them.
MIN_MEANINGFUL_TILE_BYTES = 4096


def cell_of(key: str) -> tuple[float, float, float, float]:
    """The south-west corner and size of a tile's own cell, in degrees."""
    level, tile_id = level_of(key), id_of(key)
    degrees = LEVEL_DEGREES[level]
    cols = int(360.0 / degrees)
    row, col = divmod(tile_id, cols)
    lat = row * degrees - 90.0
    lon = col * degrees - 180.0
    return lat, lon, lat + degrees, lon + degrees


def bounds_of(keys: list[str], sizes: dict[str, int]) -> dict:
    """The ground a cluster covers, as a WGS84 box.

    Published because an asset's id range is a *span*, and ids run row-major: a
    span from the westernmost alpine tile to the easternmost passes through
    every id in the rows between, which is ground a thousand kilometres away.
    Without this the client would treat an alpine cluster as a candidate for a
    drive in Brittany -- the index would still refuse the tiles, so nothing
    would route wrongly, but it would fetch the wrong index to find out and tell
    the driver the wrong thing about their map.
    """
    # Level 2 only. A level-0 cell is 4 degrees across and a level-1 cell one
    # degree, so those always overhang the ground actually built: unioning every
    # level put the western Alps in a box reaching Portugal, which is precisely
    # the over-claiming this field exists to prevent. The fine hierarchy is the
    # extract's real footprint. Coarse tiles are still served, because a key is
    # tested by whether its own cell *overlaps* the box, and a 4-degree cell
    # covering the Alps does.
    local = [
        key for key in keys
        if level_of(key) == 2 and sizes.get(key, 0) >= MIN_MEANINGFUL_TILE_BYTES
    ] or [key for key in keys if level_of(key) == 2] or list(keys)
    cells = [cell_of(key) for key in local]
    return {
        "minLat": min(c[0] for c in cells),
        "minLon": min(c[1] for c in cells),
        "maxLat": max(c[2] for c in cells),
        "maxLon": max(c[3] for c in cells),
    }


def ranges_of(keys: list[str]) -> list[dict]:
    """Per-level ``lo``/``hi`` tile ids for an asset, inclusive.

    This is what lets the manifest answer "which asset holds this cell" without
    shipping a map of every key: assets are filled in sorted order, so each one
    holds a contiguous run of ids at each level it touches.
    """
    out = []
    for level in sorted({level_of(k) for k in keys}):
        ids = [id_of(k) for k in keys if level_of(k) == level]
        out.append({"l": level, "lo": min(ids), "hi": max(ids)})
    return out


def tiles_from_dir(root: Path) -> list[tuple[str, Path]]:
    """Every ``.gph`` under a built tile tree, keyed as the index keys them."""
    found = []
    for path in root.rglob("*.gph"):
        key = tile_key(str(path))
        if key:
            found.append((key, path))
    return found


def slice_cluster(
    tiles_root: Path,
    cluster: str,
    out_dir: Path,
    build: str,
    max_bytes: int = MAX_ASSET_BYTES,
    gzip_level: int = 6,
) -> dict:
    """Cuts one built tile tree into release assets, and describes them.

    Returns the cluster fragment merged into ``regions-v3.json``. The tiles are
    not touched: this is the same packing the per-region path does, stopped and
    restarted whenever an asset approaches the release ceiling.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    found = tiles_from_dir(tiles_root)
    if not found:
        raise SystemExit(f"no .gph tiles under {tiles_root}")
    found.sort(key=lambda kv: sort_key(kv[0]))

    assets: list[dict] = []
    index: dict[str, dict] = {}
    keys_in_asset: list[str] = []
    tf: tarfile.TarFile | None = None
    vtar_path: Path | None = None

    def finish() -> None:
        nonlocal tf, index, keys_in_asset, vtar_path
        if tf is None or vtar_path is None:
            return
        tf.close()
        name = vtar_path.stem
        doc = {
            "build": build,
            "cluster": cluster,
            "region": name,
            "tileCount": len(index),
            "vtarBytes": vtar_path.stat().st_size,
            "rawBytes": sum(v["u"] for v in index.values()),
            "tiles": index,
        }
        (out_dir / f"{name}.idx.json").write_text(
            json.dumps(doc, separators=(",", ":"), sort_keys=True)
        )
        assets.append({
            "vtar": f"{name}.vtar",
            "idx": f"{name}.idx.json",
            "tileCount": doc["tileCount"],
            "vtarBytes": doc["vtarBytes"],
            "ranges": ranges_of(keys_in_asset),
        })
        print(f"  {name}: {doc['tileCount']} tiles, {doc['vtarBytes']/1e6:.0f} MB")
        tf, index, keys_in_asset, vtar_path = None, {}, [], None

    for key, path in found:
        raw = path.read_bytes()
        blob = gzip_blob(raw, gzip_level)
        # Rolling over *before* the write is what keeps the promise: an asset
        # never exceeds the ceiling, rather than exceeding it by one tile and
        # failing the upload hours later. A tile larger than the cap on its own
        # still gets an asset of its own rather than no asset at all.
        if tf is not None and vtar_path is not None:
            if tf.fileobj.tell() + len(blob) + 1024 > max_bytes:
                finish()
        if tf is None:
            vtar_path = out_dir / f"{cluster}.{len(assets):02d}.vtar"
            tf = tarfile.open(vtar_path, "w", format=tarfile.GNU_FORMAT)
        index[key] = add_tile(tf, key, blob, raw)
        keys_in_asset.append(key)

    finish()

    fragment = {
        "id": cluster,
        "build": build,
        "bounds": bounds_of(
            [key for key, _ in found],
            {key: entry["u"] for asset in assets for key, entry in
             json.loads((out_dir / asset["idx"]).read_text())["tiles"].items()},
        ),
        "assets": assets,
    }
    (out_dir / f"{cluster}.cluster.json").write_text(json.dumps(fragment, indent=2, sort_keys=True))
    verify_slices(out_dir, fragment, {k for k, _ in found})
    return fragment


def verify_slices(out_dir: Path, fragment: dict, expected: set[str]) -> None:
    """Proves the slice lost nothing, duplicated nothing and stayed in its ranges.

    A tile that lands in no asset is a hole in the middle of a cluster that looks
    exactly like ordinary coverage: the client asks the manifest which asset
    holds the cell, is told none, and reports the route as uncovered there. That
    is indistinguishable from the edge of the world, so it has to be impossible
    rather than unlikely.
    """
    seen: dict[str, str] = {}
    for asset in fragment["assets"]:
        doc = json.loads((out_dir / asset["idx"]).read_text())
        spans = {r["l"]: (r["lo"], r["hi"]) for r in asset["ranges"]}
        for key in doc["tiles"]:
            if key in seen:
                raise SystemExit(f"{key} is in both {seen[key]} and {asset['vtar']}")
            seen[key] = asset["vtar"]
            lo, hi = spans[level_of(key)]
            if not lo <= id_of(key) <= hi:
                raise SystemExit(f"{key} sits outside {asset['vtar']}'s declared range")

    missing = expected - set(seen)
    if missing:
        raise SystemExit(f"{len(missing)} tile(s) landed in no asset, e.g. {sorted(missing)[:3]}")

    # Ranges must not overlap between assets at a level, or "which asset holds
    # this cell" has two answers and the client picks by accident.
    for level in {r["l"] for a in fragment["assets"] for r in a["ranges"]}:
        spans = sorted(
            ((r["lo"], r["hi"], a["vtar"]) for a in fragment["assets"] for r in a["ranges"] if r["l"] == level)
        )
        for (lo1, hi1, a1), (lo2, hi2, a2) in zip(spans, spans[1:]):
            if lo2 <= hi1:
                raise SystemExit(f"level {level}: {a1} and {a2} both claim id {lo2}")

    total = sum(a["vtarBytes"] for a in fragment["assets"])
    print(f"  verified {len(seen)} tiles across {len(fragment['assets'])} asset(s), {total/1e6:.0f} MB total")


def repack(zip_path: Path, region: str, out_dir: Path, gzip_level: int = 6) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    vtar_path = out_dir / f"{region}.vtar"
    idx_path = out_dir / f"{region}.idx.json"

    index: dict[str, dict] = {}
    skipped: list[str] = []

    with zipfile.ZipFile(zip_path) as zf, tarfile.open(vtar_path, "w", format=tarfile.GNU_FORMAT) as tf:
        entries = [i for i in zf.infolist() if not i.is_dir()]
        tiles = [(tile_key(i.filename), i) for i in entries]
        skipped = [i.filename for k, i in tiles if k is None]
        # Sorted so a rebuild is byte-reproducible and diffs are meaningful.
        tiles = sorted(((k, i) for k, i in tiles if k), key=lambda kv: kv[0])

        for key, info in tiles:
            raw = zf.read(info)
            index[key] = add_tile(tf, key, gzip_blob(raw, gzip_level), raw)

    doc = {
        "build": BUILD_ID,
        "region": region,
        "tileCount": len(index),
        "vtarBytes": vtar_path.stat().st_size,
        "rawBytes": sum(v["u"] for v in index.values()),
        "tiles": index,
    }
    idx_path.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=True))

    if skipped:
        print(f"  note: {len(skipped)} non-tile entries skipped (e.g. {skipped[0]})")
    return doc


def verify(vtar_path: Path, idx_path: Path, sample: int = 12) -> None:
    """Re-read tiles through their recorded byte ranges, exactly as a client does.

    This is the step that catches an off-by-one in the offset arithmetic, which
    would otherwise surface as a corrupt tile on a driver's phone.
    """
    doc = json.loads(idx_path.read_text())
    keys = list(doc["tiles"])
    step = max(1, len(keys) // sample)
    checked = 0
    with open(vtar_path, "rb") as f:
        for key in keys[::step]:
            e = doc["tiles"][key]
            f.seek(e["o"])
            blob = f.read(e["c"])
            raw = gzip.decompress(blob)
            assert len(raw) == e["u"], f"{key}: size {len(raw)} != {e['u']}"
            got = "sha256:" + hashlib.sha256(raw).hexdigest()
            assert got == e["h"], f"{key}: checksum mismatch"
            checked += 1
    print(f"  verified {checked} tiles by byte range (offset arithmetic is sound)")


def fetch_zip(region: str, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{region}.vtiles.zip"
    if dest.exists():
        return dest
    url = MAPS_V2_BASE + f"{region}.vtiles.zip"
    print(f"  downloading {url}")
    with urllib.request.urlopen(url) as r, open(dest, "wb") as out:
        while chunk := r.read(1 << 20):
            out.write(chunk)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", type=Path, help="a local maps-v2 zip")
    ap.add_argument("--region", help="region id, e.g. ch_full")
    ap.add_argument("--all", action="store_true", help="every region in regions.json")
    ap.add_argument("--tiles", type=Path, help="a built tile tree to slice into cluster assets")
    ap.add_argument("--cluster", help="cluster id the --tiles tree belongs to, e.g. eu_alps")
    ap.add_argument("--build", help="build id to stamp; defaults to <cluster>-<BUILD_ID date>")
    ap.add_argument("--max-asset-bytes", type=int, default=MAX_ASSET_BYTES)
    ap.add_argument("--regions-json", type=Path, default=Path(__file__).resolve().parents[2] / "regions.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cache", type=Path, default=Path("./_v2cache"))
    ap.add_argument("--gzip-level", type=int, default=6)
    args = ap.parse_args()

    if args.tiles or args.cluster:
        if not (args.tiles and args.cluster):
            ap.error("--tiles and --cluster go together")
        # Each cluster carries its own build id. That is what keeps its tiles in
        # their own pool on the phone, so two clusters that publish the same
        # border cell from different builds can never end up in one directory --
        # structurally impossible rather than merely discouraged.
        build = args.build or f"{args.cluster}-{BUILD_ID.split('-', 1)[1]}"
        print(f"[{args.cluster}] slicing {args.tiles} as build {build}")
        fragment = slice_cluster(
            args.tiles, args.cluster, args.out, build,
            max_bytes=args.max_asset_bytes, gzip_level=args.gzip_level,
        )
        print(f"\nwrote {len(fragment['assets'])} asset(s) + {args.cluster}.cluster.json to {args.out}")
        return 0

    targets: list[tuple[str, Path | None]] = []
    if args.all:
        catalog = json.loads(args.regions_json.read_text())
        targets = [(r["id"], None) for r in catalog]
    elif args.region:
        targets = [(args.region, args.zip)]
    else:
        ap.error("pass --region (with optional --zip) or --all")

    manifest = []
    for region, zip_path in targets:
        print(f"[{region}]")
        path = zip_path if zip_path else fetch_zip(region, args.cache)
        doc = repack(path, region, args.out, args.gzip_level)
        verify(args.out / f"{region}.vtar", args.out / f"{region}.idx.json")
        ratio = 100 * doc["vtarBytes"] / max(1, doc["rawBytes"])
        print(f"  {doc['tileCount']} tiles  raw {doc['rawBytes']/1e6:.1f} MB"
              f"  -> vtar {doc['vtarBytes']/1e6:.1f} MB ({ratio:.0f}%)")
        manifest.append({
            "id": region,
            "build": BUILD_ID,
            "vtar": f"{region}.vtar",
            "idx": f"{region}.idx.json",
            "tileCount": doc["tileCount"],
            "vtarBytes": doc["vtarBytes"],
        })

    (args.out / "regions-v3.json").write_text(
        json.dumps({"build": BUILD_ID, "regions": manifest}, indent=2, sort_keys=True)
    )
    print(f"\nwrote {len(manifest)} region(s) + regions-v3.json to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
