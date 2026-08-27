#!/usr/bin/env python3
"""
Check that every border a Trek could drive across is inside some cluster.

Why this is a script and not a habit
------------------------------------

A cluster is the only thing that makes a border crossing work: tiles built in
separate runs disagree about the cell they share, so two countries a driver can
cross between have to be built together or the crossing cannot be navigated
offline. That makes "which countries are in a cluster together" a correctness
property of the published data, and it is not one anybody can hold in their
head — 51 regions have 73 adjacent pairs.

So the adjacency comes from the same bounding boxes the client resolves regions
with (`MapRegion.BOUNDS`), and every land-adjacent pair must appear together in
at least one cluster. Adding a region to the app without adding it to a cluster
now produces a listed gap instead of a Trek that goes guidance-blind at a
frontier nobody thought about.

Water crossings are listed explicitly rather than detected. Two boxes can touch
across a strait, and a driver cannot: Australia and Indonesia share no road, and
France to the UK is a train. Being wrong about one of these costs a needless
continental build, so they are named and justified rather than inferred.

Usage
-----

    python3 audit_clusters.py                 # report gaps, exit 1 if any
    python3 audit_clusters.py --list-pairs    # every adjacency and its cluster
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "regions.v3.json"

# The client's own boxes, read from the client. Duplicating them here would let
# the two drift, and the drift would be invisible until someone crossed a border
# the audit believed did not exist.
MAP_REGION_KT = (
    HERE.parents[2]
    / "rally-timer-android/shared/src/commonMain/kotlin/ch/vetroster/rallytimer/shared/model/MapRegion.kt"
)

# Pairs whose boxes touch but whose roads do not. Each one is a claim about the
# world, so each one carries its reason.
SEA_BORDERS = {
    ("au_full", "id_full"): "Timor and Arafura seas",
    ("de_full", "se_full"): "Baltic; the road runs through Denmark",
    ("dk_full", "pl_full"): "Baltic",
    ("es_full", "ma_full"): "Strait of Gibraltar; Ceuta and Melilla are enclaves",
    ("fr_full", "ie_full"): "Celtic Sea",
    ("fr_full", "uk_full"): "Channel; the tunnel is a car-carrying train",
    ("cz_full", "hu_full"): "not adjacent at all — the boxes overlap over Slovakia",
    ("hr_full", "it_full"): "the Adriatic; the road between them runs through Slovenia",
    ("it_full", "rs_full"): "not adjacent at all — Italy's box clears Serbia's by 0.3 degrees of open sea",
    ("ba_full", "it_full"): "the Adriatic",
    ("it_full", "me_full"): "the Adriatic",
    ("al_full", "ba_full"): "not adjacent — Montenegro and Kosovo lie between them",
    ("al_full", "hr_full"): "not adjacent — Montenegro lies between them",
    ("ba_full", "si_full"): "not adjacent — Croatia lies between them",
    ("gr_full", "me_full"): "not adjacent — Albania lies between them",
    ("me_full", "ro_full"): "not adjacent — Serbia lies between them",
    ("br_full", "cl_full"): "not adjacent — the Andes and Bolivia lie between them; Brazil's box is simply enormous",
}


def region_bounds() -> dict[str, list[tuple[float, float, float, float]]]:
    if not MAP_REGION_KT.exists():
        raise SystemExit(
            f"cannot read {MAP_REGION_KT}.\n"
            "This audit reads the client's own region boxes, so the app repository "
            "has to be checked out beside this one."
        )
    text = MAP_REGION_KT.read_text()
    block = text.split("val BOUNDS: Map<String, List<MapRegionBounds>>")[1]

    # Split on the region headers and read each region's whole body, rather than
    # matching up to the first `),`. A concave country is declared over several
    # lines -- Austria and Italy both are -- and a non-greedy match ends at the
    # first box's closing paren, leaving a fragment with no complete
    # MapRegionBounds in it. Those regions then vanish from the audit entirely:
    # it reported 51 regions where the client has 53, and every border touching
    # Austria or Italy went unchecked. An audit that silently drops the two
    # hardest-shaped countries is worse than no audit, because it reports
    # success.
    out: dict[str, list[tuple[float, float, float, float]]] = {}
    headers = list(re.finditer(r'"(\w+)" to listOf\(', block))
    for index, match in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(block)
        body = block[match.end():end]
        boxes = [
            tuple(float(v) for v in box)
            for box in re.findall(
                r"MapRegionBounds\(([-\d.]+),\s*([-\d.]+),\s*([-\d.]+),\s*([-\d.]+)\)",
                body,
            )
        ]
        if boxes:
            out[match.group(1)] = boxes
    return out


def adjacent(bounds, a: str, b: str, pad: float = 0.35) -> bool:
    """True when two regions' boxes come within roughly 35 km of each other.

    Padded because the boxes are generous approximations of coastlines and
    frontiers; a border that a box misses by a few kilometres is still a border.
    """
    for a_min_lat, a_min_lon, a_max_lat, a_max_lon in bounds[a]:
        for b_min_lat, b_min_lon, b_max_lat, b_max_lon in bounds[b]:
            if (
                a_min_lat - pad < b_max_lat
                and b_min_lat - pad < a_max_lat
                and a_min_lon - pad < b_max_lon
                and b_min_lon - pad < a_max_lon
            ):
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-pairs", action="store_true")
    args = ap.parse_args()

    bounds = region_bounds()
    clusters = {c["cluster"]: set(c["ids"]) for c in json.loads(SPEC.read_text())["clusters"]}

    ids = sorted(bounds)
    pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:] if adjacent(bounds, a, b)]

    gaps: list[tuple[str, str]] = []
    for a, b in pairs:
        if (a, b) in SEA_BORDERS:
            if args.list_pairs:
                print(f"  {a:16s} {b:16s} sea — {SEA_BORDERS[(a, b)]}")
            continue
        holder = next((name for name, members in clusters.items() if a in members and b in members), None)
        if holder is None:
            gaps.append((a, b))
        elif args.list_pairs:
            print(f"  {a:16s} {b:16s} {holder}")

    land = len(pairs) - sum(1 for pair in pairs if pair in SEA_BORDERS)
    print(
        f"\n{len(ids)} regions, {len(pairs)} adjacent pairs "
        f"({land} by road), {len(clusters)} clusters"
    )

    if not gaps:
        print("every land border is inside a cluster")
        return 0

    print(f"\n{len(gaps)} land border(s) no cluster covers — a Trek across one of these")
    print("cannot be navigated offline, whatever is published:\n")
    for a, b in gaps:
        print(f"  {a:16s} {b}")
    print("\nFix by adding both ids to a cluster in regions.v3.json (and its extract),")
    print("or by listing the pair in SEA_BORDERS here with the reason it is not a road.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
