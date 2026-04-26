# Valhalla v2 Tiles

This folder contains the Docker-based builder for the `maps-v2` Valhalla release used by RallyTimer iOS.

The current `ghcr.io/valhalla/valhalla:latest` image was stable here only with single-threaded tile builds, so `build_tiles.ps1` defaults to `-Concurrency 1`.

## Files

- `regions.v2.json`: canonical v2 region catalog copied from the app-side expectations and matched to Geofabrik extracts
- `build_tiles.ps1`: Windows-friendly builder that downloads PBFs, runs `ghcr.io/valhalla/valhalla:latest`, zips the resulting tiles, and writes a merged `regions.json`

## Examples

Build one missing region:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\valhalla-tiles\build_tiles.ps1 -Region si_full
```

Build only the regions still missing from the public `maps-v2` release:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\valhalla-tiles\build_tiles.ps1 -MissingOnly
```

Use a different concurrency only if a region builds reliably in your environment:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\valhalla-tiles\build_tiles.ps1 -Region si_full -Concurrency 2
```

Keep downloaded PBFs and the unzipped tile directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\valhalla-tiles\build_tiles.ps1 -Region si_full -KeepPbf -KeepTiles
```

Outputs are written under `tools/valhalla-tiles/output/`:

- `pbf/`: downloaded Geofabrik extracts
- `tiles/`: per-region temporary Valhalla build workspace
- `zips/`: final `*.vtiles.zip` archives
- `regions.json`: merged release manifest using published regions plus any locally-built zips
- `sizes.csv`: size report for the available zips
- `build_*.log`: timestamped build logs
