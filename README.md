# regularityMateMaps

Offline routing tiles for [Regularity Mate](https://www.regularitymate.com).
The app downloads only the tiles a route actually needs; this repository holds
the tooling that produces them and the releases that serve them.

## Licence and attribution

The tiles published here are a **derived database of OpenStreetMap data** and
are distributed under the **Open Database License (ODbL) v1.0**, the same
licence as the source data.

> Contains information from [OpenStreetMap](https://www.openstreetmap.org),
> which is made available under the
> [Open Database License (ODbL) v1.0](https://opendatacommons.org/licenses/odbl/1-0/).
> © OpenStreetMap contributors.

What that means in practice, for anyone using these assets:

- **Attribute.** Credit "© OpenStreetMap contributors" wherever the data or
  anything computed from it is shown to a person. Regularity Mate does this on
  its map views and in the offline-maps screen.
- **Share alike.** A modified or extended version of this database must be
  offered under ODbL as well.
- **Keep the notice.** This section travels with the data; see `ATTRIBUTION.txt`,
  which is published alongside every release.

Source data comes from [Geofabrik](https://download.geofabrik.de/)'s extracts of
OpenStreetMap. The tiles are built with [Valhalla](https://github.com/valhalla/valhalla)
(MIT). The tooling in `tools/` is part of the Regularity Mate project.

## What is published

Releases are tags, and each tag is a format rather than a date.

| Tag | Contents |
|---|---|
| `maps-v2` | One zip per country. Whole-country downloads only — Switzerland is 227 MB, Germany 1.76 GB |
| `maps-v3` | Per-tile addressable assets. A 46 km alpine route costs ~10 MB instead of 227 MB |

`maps-v3` holds two kinds of asset, plus one manifest:

```
<region>.vtar          a country, built alone
<cluster>.NN.vtar      one slice of a cluster: many countries, built together
<name>.idx.json        which tile lives at which byte range, with a sha256 each
regions-v3.json        the manifest — the only discovery path
```

Every `.vtar` is an uncompressed POSIX tar whose members are individually
gzipped tiles, so a client can range-GET one tile without downloading the
container. `tar tf` works on them by hand.

### Countries, and why clusters exist

Valhalla tiles are not independent. A directed edge stores its end node as a
GraphId carrying the *neighbour tile's node index*, and that index is assigned
while that tile is built. Build France alone and Italy alone and the cell they
share along the border gets two different node orderings — so a route drawn from
both sets of tiles follows whatever node now happens to sit at that index. Wrong
roads, no error, nothing logged.

A **cluster** is therefore several countries built in one `valhalla_build_tiles`
run over one merged extract, and sliced afterwards into assets small enough for a
release. Slicing is packaging only: everything inside a cluster descends from a
single build, so every border inside it joins up. Each cluster carries its own
build id, which keeps its tiles in their own directory on the phone and makes
mixing two clusters impossible rather than merely discouraged.

Clusters are defined in `tools/valhalla-tiles/regions.v3.json`.

## Building

Tiles are built car-only — `--mjolnir-include-pedestrian false
--mjolnir-include-bicycle false`. Measured on the real Swiss extract that is 42%
smaller overall and 46% smaller at level 2, where a corridor's bytes are, with
identical routing for driving: same road, same distance, same instructions.
Service roads, tracks and unclassified roads are all kept, because a rally route
can legitimately run over a farm track.

| Job | Where |
|---|---|
| `repack-v3.yml` | Re-containers existing `maps-v2` tiles. Fast, same tile content |
| `build-v3.yml` | Rebuilds one country car-only from a Geofabrik extract |
| `build-cluster.yml` | Builds a cluster small enough for a free runner (`eu_isles`, `sa_cone`) |
| `tools/valhalla-tiles/build_cluster.sh` | Builds any cluster on a workstation. Resumable |

A free GitHub runner caps a job at six hours and offers ~55 GB of disk. The
large clusters do not fit — `eu_alps` merges roughly 11 GB of extracts — so they
are built locally with the script, which produces the same assets and can be
interrupted without losing the hours already spent:

```bash
WORK=/Volumes/SomeDisk/tilebuild ./tools/valhalla-tiles/build_cluster.sh eu_alps
gh release upload maps-v3 "$WORK"/packed/eu_alps.*.vtar "$WORK"/packed/eu_alps.*.idx.json --clobber
python3 tools/valhalla-tiles/merge_manifest.py --out regions-v3.json
gh release upload maps-v3 regions-v3.json --clobber
```

`merge_manifest.py` rebuilds `regions-v3.json` from the assets actually on the
release rather than editing a downloaded copy — a half-failed merge once
published 37 regions and listed two.

## Bumping a build

A build id change invalidates every phone's pool for that build, because tiles
from two builds sitting next to each other route wrongly rather than failing.
Bump rarely — quarterly at most — and never for a cosmetic change. A cluster
bumps as a whole; publishing half a cluster at a new build id is refused by
`merge_manifest.py`, which is the one error worth failing a release over.
