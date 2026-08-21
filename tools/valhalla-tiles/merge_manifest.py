#!/usr/bin/env python3
"""
Rebuild ``regions-v3.json`` from whatever is actually on the release.

``regions-v3.json`` is the only discovery path: an asset no client can look up
is invisible, and a manifest entry pointing at an asset that is not there is
worse than invisible. So this is derived from the release listing rather than
merged onto a previously downloaded copy -- a merge that half-failed once
published 37 regions and listed two, and ``--clobber`` made that permanent.

It describes two kinds of asset:

**Per-country builds** (``ch_full.vtar``) are one country, built alone. They
serve the whole-country download and corridors that stay inside one country.

**Cluster assets** (``eu_alps.00.vtar``) are slices of one build covering many
countries. They are what makes a border crossing work: everything in a cluster
comes from a single ``valhalla_build_tiles`` run, so the node indices that
Valhalla's cross-tile edges carry agree all the way across it.

A cluster's assets are described by the tile-id ranges each one holds, derived
here from the indexes themselves rather than trusted from a side file. That is
what lets a phone work out which one or two assets a corridor needs and fetch
only those indexes, instead of pulling every index in the cluster to find out.

Usage
-----

    python3 merge_manifest.py --out regions-v3.json
    python3 merge_manifest.py --out regions-v3.json --extra ./packed
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

BUILD_ID = "v3-2026-08"
SPEC = Path(__file__).resolve().parent / "regions.v3.json"


def release_assets(tag: str) -> dict[str, int]:
    listing = json.loads(subprocess.run(
        ["gh", "release", "view", tag, "--json", "assets"],
        check=True, capture_output=True, text=True,
    ).stdout)["assets"]
    return {a["name"]: a["size"] for a in listing}


def fetch_index(tag: str, name: str, into: Path) -> dict:
    subprocess.run(
        ["gh", "release", "download", tag, "-p", name, "-D", str(into), "--clobber"],
        check=True, capture_output=True,
    )
    return json.loads((into / name).read_text())


def ranges_of(index: dict) -> list[dict]:
    """Per-level ``lo``/``hi`` tile ids an asset holds, inclusive.

    Derived from the index rather than read from the packer's side file, so a
    manifest can be rebuilt from the release alone -- which is the whole point
    of rebuilding it from the release.
    """
    by_level: dict[int, list[int]] = defaultdict(list)
    for key in index["tiles"]:
        level, tile_id = key.split("/")
        by_level[int(level)].append(int(tile_id))
    return [
        {"l": level, "lo": min(ids), "hi": max(ids)}
        for level, ids in sorted(by_level.items())
    ]


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


def bounds_of(index: dict) -> dict:
    """The ground an asset's tiles cover, as a WGS84 box.

    Derived here rather than trusted from the packer's side file, so a manifest
    can be rebuilt from the release alone.
    """
    # Level 2 only: coarse cells are 4 and 1 degrees across and always overhang
    # the ground actually built, which would inflate the box until it claimed
    # countries the cluster has never held.
    keys = [
        k for k, v in index["tiles"].items()
        if k.startswith("2/") and v.get("u", 0) >= MIN_MEANINGFUL_TILE_BYTES
    ] or [k for k in index["tiles"] if k.startswith("2/")] or list(index["tiles"])
    lats, lons = [], []
    for key in keys:
        level, tile_id = key.split("/")
        degrees = LEVEL_DEGREES[int(level)]
        cols = int(360.0 / degrees)
        row, col = divmod(int(tile_id), cols)
        lats += [row * degrees - 90.0, row * degrees - 90.0 + degrees]
        lons += [col * degrees - 180.0, col * degrees - 180.0 + degrees]
    return {"minLat": min(lats), "minLon": min(lons), "maxLat": max(lats), "maxLon": max(lons)}


def widen(box: dict | None, other: dict) -> dict:
    if box is None:
        return dict(other)
    return {
        "minLat": min(box["minLat"], other["minLat"]),
        "minLon": min(box["minLon"], other["minLon"]),
        "maxLat": max(box["maxLat"], other["maxLat"]),
        "maxLon": max(box["maxLon"], other["maxLon"]),
    }


def build(tag: str, extra: Path | None) -> dict:
    spec = json.loads(SPEC.read_text())
    ids_for = {b["build"]: b["ids"] for b in spec["builds"]}
    cluster_ids = {c["cluster"]: c["ids"] for c in spec.get("clusters", [])}

    sizes = release_assets(tag)
    names = sorted(n for n in sizes if n.endswith(".idx.json"))

    regions: list[dict] = []
    clusters: dict[str, dict] = {}

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for name in names:
            asset = name[: -len(".idx.json")]
            # An index whose vtar is absent describes nothing fetchable. The
            # 2 GiB asset ceiling leaves exactly that behind when an upload is
            # rejected, and listing it would send clients after a 404.
            if f"{asset}.vtar" not in sizes:
                print(f"skipping {asset}: no vtar on the release")
                continue

            local = extra / name if extra and (extra / name).exists() else None
            index = json.loads(local.read_text()) if local else fetch_index(tag, name, work)

            entry = {
                "vtar": f"{asset}.vtar",
                "idx": name,
                "tileCount": index["tileCount"],
                "vtarBytes": index.get("vtarBytes", sizes[f"{asset}.vtar"]),
            }

            cluster = index.get("cluster")
            if cluster:
                slot = clusters.setdefault(cluster, {
                    "id": cluster,
                    "build": index["build"],
                    "regions": cluster_ids.get(cluster, []),
                    "bounds": None,
                    "assets": [],
                })
                if slot["build"] != index["build"]:
                    # Two builds of one cluster on the release at once means half
                    # its assets carry node indices the other half disagrees with:
                    # the exact silent-wrong-routing failure clusters exist to
                    # prevent. Refusing to publish is the only safe answer.
                    raise SystemExit(
                        f"{cluster}: asset {asset} is build {index['build']}, "
                        f"but the cluster is {slot['build']}. Re-slice or delete the stale assets."
                    )
                slot["assets"].append({**entry, "ranges": ranges_of(index)})
                slot["bounds"] = widen(slot.get("bounds"), bounds_of(index))
            else:
                for rid in ids_for.get(asset, [asset]):
                    regions.append({"id": rid, "build": index["build"], **entry})

    for slot in clusters.values():
        slot["assets"].sort(key=lambda a: a["vtar"])
        if not slot["regions"]:
            print(f"note: cluster {slot['id']} names no region ids in regions.v3.json")

    return {
        "build": BUILD_ID,
        "regions": sorted(regions, key=lambda r: r["id"]),
        "clusters": [clusters[k] for k in sorted(clusters)],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default="maps-v3")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--extra", type=Path, help="a local packed/ directory to read indexes from instead of downloading")
    args = ap.parse_args()

    doc = build(args.tag, args.extra)
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    tiles = sum(a["tileCount"] for c in doc["clusters"] for a in c["assets"])
    print(
        f"wrote {args.out}: {len(doc['regions'])} region id(s), "
        f"{len(doc['clusters'])} cluster(s) holding {tiles} tiles"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
