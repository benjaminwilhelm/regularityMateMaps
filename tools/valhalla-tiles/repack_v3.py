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

Usage
-----

    python3 repack_v3.py --zip ch_full.vtiles.zip --region ch_full --out ./out
    python3 repack_v3.py --all --out ./out          # every region in regions.json
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
            # mtime=0 keeps the gzip header deterministic; without it the same
            # input produces a different archive on every run.
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=gzip_level, mtime=0) as gz:
                gz.write(raw)
            blob = buf.getvalue()

            ti = tarfile.TarInfo(member_name(key))
            ti.size = len(blob)
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tf.addfile(ti, io.BytesIO(blob))

            # tar writes a 512-byte header immediately before the payload, so the
            # payload begins at the stream position after addfile minus its own
            # padded length. Recording the *payload* offset is what lets a client
            # range-GET the bytes alone and skip tar parsing entirely.
            end = tf.fileobj.tell()
            padded = (len(blob) + 511) // 512 * 512
            offset = end - padded

            index[key] = {
                "o": offset,
                "c": len(blob),
                "u": len(raw),
                "h": "sha256:" + hashlib.sha256(raw).hexdigest(),
            }

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
    ap.add_argument("--regions-json", type=Path, default=Path(__file__).resolve().parents[2] / "regions.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cache", type=Path, default=Path("./_v2cache"))
    ap.add_argument("--gzip-level", type=int, default=6)
    args = ap.parse_args()

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
