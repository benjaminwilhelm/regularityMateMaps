#!/usr/bin/env bash
#
# Build one cluster's Valhalla tiles and slice them into release assets.
#
# A cluster is several countries built *together*, in one valhalla_build_tiles
# run over one merged extract. That is not an optimisation. Valhalla tiles are
# not independent: a directed edge stores its end node as a GraphId carrying the
# neighbour tile's node index, and that index is handed out while that tile is
# built. Countries built separately number the border cell they share
# differently, so a corridor drawn from both routes onto whatever node now sits
# at that index -- wrong roads, and nothing logged. Building them together is
# the only way a Trek can cross a border offline.
#
# Why this is a script and not a workflow
# ---------------------------------------
#
# GitHub's free runners cap a job at 6 hours and offer ~55 GB of disk after the
# cleanup step. eu_alps merges roughly 11 GB of extracts and takes far longer
# than that. Small clusters (eu_isles, sa_cone) do fit CI -- see
# build-cluster.yml. Everything else is built here, on a machine you own, which
# is also the only way this costs nothing.
#
# It is resumable by design. Every stage skips itself if its output is already
# there, so a build interrupted after nine hours of tiling resumes at the slice
# rather than at the download.
#
# Usage
# -----
#
#   ./build_cluster.sh eu_alps                 # full build
#   WORK=/Volumes/KINGSTON/tilebuild ./build_cluster.sh eu_alps
#   ./build_cluster.sh eu_alps --slice-only    # re-slice tiles already built
#
# Then upload what it leaves in $WORK/packed:
#
#   gh release upload maps-v3 $WORK/packed/eu_alps.*.vtar $WORK/packed/eu_alps.*.idx.json
#   python3 merge_manifest.py --release maps-v3 --out regions-v3.json
#
set -euo pipefail

CLUSTER="${1:-}"
if [ -z "$CLUSTER" ] || [ "$CLUSTER" = "-h" ] || [ "$CLUSTER" = "--help" ]; then
  sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi
SLICE_ONLY="${2:-}"

HERE="$(cd "$(dirname "$0")" && pwd)"
SPEC="$HERE/regions.v3.json"
WORK="${WORK:-$PWD/_cluster/$CLUSTER}"
mkdir -p "$WORK"/{extracts,packed}

# --- what this cluster is made of -------------------------------------------

# read -a rather than readarray: macOS still ships bash 3.2, where readarray
# does not exist and the script would fail on its first line of real work.
BBOX="$(python3 -c "
import json
spec = json.load(open('$SPEC'))
for c in spec['clusters']:
    if c['cluster'] == '$CLUSTER':
        print(c.get('bbox', '')); break
" 2>/dev/null)"

EXTRACTS=()
while IFS= read -r line; do
  EXTRACTS+=("$line")
done < <(python3 -c "
import json, sys
spec = json.load(open('$SPEC'))
for c in spec['clusters']:
    if c['cluster'] == '$CLUSTER':
        print('\n'.join(c['geofabrik'])); break
else:
    sys.exit(\"unknown cluster '$CLUSTER'; known: \" + ', '.join(c['cluster'] for c in spec['clusters']))
")

# The lookup above runs in a process substitution, where a non-zero exit is
# invisible to set -e. Without this check an unknown cluster prints its error
# and then carries on into an unbound-variable failure ten lines later.
if [ "${#EXTRACTS[@]}" -eq 0 ]; then
  echo "no extracts for cluster '$CLUSTER' (see the message above)" >&2
  exit 1
fi

# Advisory, not a gate: a gap elsewhere in the world is no reason to refuse the
# cluster in hand, but the moment before spending hours on a build is the right
# moment to be told which borders still have none.
python3 "$HERE/audit_clusters.py" >/dev/null 2>&1 || {
  echo
  python3 "$HERE/audit_clusters.py" 2>&1 | tail -n +2 || true
  echo
}

echo "cluster $CLUSTER: ${#EXTRACTS[@]} extract(s)"
printf '  %s\n' "${EXTRACTS[@]}"

# Tiling wants room for the extracts, the merge, the tile tree and the packed
# assets at once. Saying so now beats running out of space eight hours in.
# -g is GB on macOS and invalid on GNU df; -k is the one both agree on.
AVAIL_GB=$(df -k "$WORK" | awk 'NR==2 {print int($4 / 1048576)}')
echo "free space at $WORK: ${AVAIL_GB} GB"
if [ "$AVAIL_GB" -lt 150 ]; then
  echo "warning: a continental cluster wants ~150 GB free; this may not finish" >&2
fi

MERGED="$WORK/$CLUSTER.osm.pbf"
TILES="$WORK/tiles"

if [ "$SLICE_ONLY" != "--slice-only" ]; then

  # --- 1. extracts ----------------------------------------------------------

  for e in "${EXTRACTS[@]}"; do
    dest="$WORK/extracts/$(echo "$e" | tr '/' '_').osm.pbf"
    if [ -s "$dest" ]; then
      echo "have $(basename "$dest")"
      continue
    fi
    echo "downloading $e"
    # -C - resumes a partial file: these are gigabytes over a home connection
    # and a dropped download should not start again from zero.
    curl -fL -C - -o "$dest" "https://download.geofabrik.de/${e}-latest.osm.pbf"
  done

  # --- 2. merge -------------------------------------------------------------

  if [ -s "$MERGED" ]; then
    echo "have $(basename "$MERGED")"
  elif [ "${#EXTRACTS[@]}" -eq 1 ]; then
    # One extract is already a merged extract. Copying rather than linking keeps
    # the resume logic honest about what exists.
    cp "$WORK/extracts/$(echo "${EXTRACTS[0]}" | tr '/' '_').osm.pbf" "$MERGED"
  else
    command -v osmium >/dev/null || { echo "osmium not found: brew install osmium-tool" >&2; exit 1; }
    echo "merging ${#EXTRACTS[@]} extracts"
    # osmium merge drops objects that appear in more than one input, which is
    # exactly what neighbouring Geofabrik extracts do along their shared border.
    # Concatenating instead would feed Valhalla the same way twice.
    osmium merge "$WORK"/extracts/*.osm.pbf -o "$MERGED" --overwrite
  fi
  # Cut to the cluster's box, when it declares one. Not a nicety: tiling is
  # bounded by memory rather than disk, and a continental extract dies inside a
  # Docker VM that has been given a fraction of the machine. --strategy
  # complete_ways keeps a road that leaves the box intact rather than severing
  # it mid-way, which would leave routing walking off the end of an edge.
  if [ -n "$BBOX" ]; then
    CUT_MARK="$WORK/.cut-$(echo "$BBOX" | tr ',' '_')"
    if [ -f "$CUT_MARK" ]; then
      echo "already cut to $BBOX"
    else
      command -v osmium >/dev/null || { echo "osmium not found: brew install osmium-tool" >&2; exit 1; }
      echo "cutting to $BBOX"
      osmium extract --bbox "$BBOX" --strategy complete_ways \
        -o "$WORK/cut.osm.pbf" "$MERGED" --overwrite
      mv "$WORK/cut.osm.pbf" "$MERGED"
      touch "$CUT_MARK"
    fi
  fi
  ls -lh "$MERGED"

  # --- 3. tiles -------------------------------------------------------------

  if [ -d "$TILES" ] && [ -n "$(find "$TILES" -name '*.gph' -print -quit)" ]; then
    echo "have tiles at $TILES"
  else
    mkdir -p "$TILES"
    CONF="$WORK/valhalla.json"
    # Car-only: measured at 42% smaller overall and 46% at level 2, where a
    # corridor's bytes are, with identical routing for driving. Valhalla's own
    # switches rather than an osmium tag filter, because filtering by hand
    # quietly drops the turn restrictions and admin boundaries that routing
    # correctness depends on.
    if command -v valhalla_build_tiles >/dev/null; then
      echo "building tiles with the local valhalla"
      valhalla_build_config --mjolnir-tile-dir "$TILES" \
        --mjolnir-include-pedestrian false \
        --mjolnir-include-bicycle false > "$CONF"
      valhalla_build_tiles -c "$CONF" "$MERGED"
    else
      command -v docker >/dev/null || {
        echo "neither valhalla_build_tiles nor docker found." >&2
        echo "On macOS prefer the native tools: a Docker Desktop VM defaults to" >&2
        echo "8 GB of RAM regardless of what the machine has, and a continental" >&2
        echo "build inside it dies hours in with no useful message." >&2
        exit 1
      }
      echo "building tiles with docker (check Docker Desktop's memory limit first)"
      docker run --rm -v "$WORK:/w" -w /w ghcr.io/valhalla/valhalla:latest sh -c \
        "valhalla_build_config --mjolnir-tile-dir /w/tiles \
           --mjolnir-include-pedestrian false \
           --mjolnir-include-bicycle false > /w/valhalla.json && \
         valhalla_build_tiles -c /w/valhalla.json /w/$(basename "$MERGED")"
    fi
  fi
  du -sh "$TILES"
fi

# --- 4. slice ---------------------------------------------------------------

python3 "$HERE/repack_v3.py" --tiles "$TILES" --cluster "$CLUSTER" --out "$WORK/packed"

echo
echo "assets are in $WORK/packed"
echo "upload with:"
echo "  gh release upload maps-v3 $WORK/packed/$CLUSTER.*.vtar $WORK/packed/$CLUSTER.*.idx.json --clobber"
echo "then rebuild the manifest:"
echo "  python3 $HERE/merge_manifest.py --out regions-v3.json && gh release upload maps-v3 regions-v3.json --clobber"
