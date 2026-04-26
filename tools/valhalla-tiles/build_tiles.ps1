[CmdletBinding()]
param(
    [string]$OutputDir,
    [string[]]$Region,
    [string]$ReleaseTag = "maps-v2",
    [int]$Concurrency = 1,
    [switch]$MissingOnly,
    [switch]$KeepPbf,
    [switch]$KeepTiles,
    [switch]$PullImage,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $PSScriptRoot "output"
}

$script:Repository = "benjaminwilhelm/regularityMateMaps"
$script:ValhallaImage = "ghcr.io/valhalla/valhalla:latest"
$script:MetadataPath = Join-Path $PSScriptRoot "regions.v2.json"
$script:OutputRoot = [System.IO.Path]::GetFullPath($OutputDir)
$script:PbfDir = Join-Path $script:OutputRoot "pbf"
$script:TilesDir = Join-Path $script:OutputRoot "tiles"
$script:ZipDir = Join-Path $script:OutputRoot "zips"
$script:ManifestPath = Join-Path $script:OutputRoot "regions.json"
$script:SizesPath = Join-Path $script:OutputRoot "sizes.csv"
$script:LogPath = Join-Path $script:OutputRoot ("build_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

New-Item -ItemType Directory -Force -Path $script:OutputRoot, $script:PbfDir, $script:TilesDir, $script:ZipDir | Out-Null
New-Item -ItemType File -Force -Path $script:LogPath | Out-Null

function Write-Log {
    param([string]$Message)

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $script:LogPath -Value $line
}

function Invoke-External {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    Write-Log ("Running: {0} {1}" -f $FilePath, ($ArgumentList -join " "))
    if ($DryRun) {
        return
    }

    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $FilePath @ArgumentList 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Write-Host $line
            Add-Content -Path $script:LogPath -Value $line
        }
    } finally {
        $ErrorActionPreference = $previousPreference
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE"
    }
}

function Get-RegionMetadata {
    if (-not (Test-Path $script:MetadataPath)) {
        throw "Missing metadata file: $script:MetadataPath"
    }

    $data = Get-Content -Path $script:MetadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($data -is [System.Array]) {
        return $data
    }

    return @($data)
}

function Get-ReleaseManifest {
    param([string]$Tag)

    $uri = "https://github.com/{0}/releases/download/{1}/regions.json" -f $script:Repository, $Tag
    try {
        $data = Invoke-RestMethod -Uri $uri -Headers @{ "User-Agent" = "regularityMateMaps-valhalla-builder" }
        if ($data -is [System.Array]) {
            return $data
        }

        return @($data)
    } catch {
        Write-Log "Release manifest not available at $uri"
        return @()
    }
}

function Get-SelectedRegions {
    param(
        [object[]]$AllRegions,
        [object[]]$ReleaseManifest,
        [string[]]$RequestedRegions,
        [bool]$MissingOnlyMode
    )

    $selected = @($AllRegions)
    if ($AllRegions.Count -eq 1 -and $AllRegions[0] -is [System.Array]) {
        $AllRegions = @($AllRegions[0])
    }

    if ($RequestedRegions -and $RequestedRegions.Count -gt 0) {
        $requested = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($regionId in $RequestedRegions) {
            [void]$requested.Add($regionId)
        }

        $selected = @($AllRegions | Where-Object { $requested.Contains([string]$_.id) })
        $matchedIds = @($selected | ForEach-Object { $_.id })
        $selectedIds = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $selected) {
            [void]$selectedIds.Add([string]$entry.id)
        }

        $missingIds = @($RequestedRegions | Where-Object { -not $selectedIds.Contains([string]$_) })
        if ($missingIds.Count -gt 0) {
            throw "Unknown region id(s): $($missingIds -join ', ')"
        }
    }

    if ($MissingOnlyMode) {
        $published = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $ReleaseManifest) {
            [void]$published.Add($entry.id)
        }

        $selected = @($selected | Where-Object { -not $published.Contains($_.id) })
    }

    return @($selected | Sort-Object id)
}

function Get-LocalZipSizeMb {
    param([string]$ZipPath)

    if (-not (Test-Path $ZipPath)) {
        return $null
    }

    return [Math]::Round(((Get-Item $ZipPath).Length / 1MB), 2)
}

function Download-Pbf {
    param([object]$RegionEntry)

    $pbfPath = Join-Path $script:PbfDir ((Split-Path $RegionEntry.geofabrikPath -Leaf) + "-latest.osm.pbf")
    if (Test-Path $pbfPath) {
        Write-Log "PBF already present: $pbfPath"
        return $pbfPath
    }

    $url = "https://download.geofabrik.de/{0}-latest.osm.pbf" -f $RegionEntry.geofabrikPath
    Write-Log "Downloading $url"
    if (-not $DryRun) {
        Invoke-External -FilePath "curl.exe" -ArgumentList @("--location", "--fail", "--silent", "--show-error", "--output", $pbfPath, $url)
    }

    return $pbfPath
}

function Build-RegionTiles {
    param([object]$RegionEntry)

    $regionRoot = Join-Path $script:TilesDir $RegionEntry.id
    $tileOutput = Join-Path $regionRoot "valhalla_tiles"
    $configPath = Join-Path $regionRoot "valhalla.json"
    $zipPath = Join-Path $script:ZipDir $RegionEntry.fileName

    $existingSize = Get-LocalZipSizeMb -ZipPath $zipPath
    if ($null -ne $existingSize) {
        Write-Log "ZIP already present for $($RegionEntry.id): $zipPath ($existingSize MB)"
        return [pscustomobject]@{ id = $RegionEntry.id; zipPath = $zipPath; fileSizeMb = $existingSize }
    }

    $pbfPath = Download-Pbf -RegionEntry $RegionEntry

    Write-Log "Building tiles for $($RegionEntry.id)"
    if (-not $DryRun) {
        Remove-Item -Recurse -Force $regionRoot -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $regionRoot | Out-Null

        $dockerCommand = "mkdir -p /work/valhalla_tiles && valhalla_build_config --mjolnir-tile-dir /work/valhalla_tiles --mjolnir-concurrency {0} > /work/valhalla.json && valhalla_build_tiles -c /work/valhalla.json /data/input.osm.pbf" -f $Concurrency
        Invoke-External -FilePath "docker" -ArgumentList @(
            "run",
            "--rm",
            "--mount", "type=bind,source=$pbfPath,target=/data/input.osm.pbf,readonly",
            "--mount", "type=bind,source=$regionRoot,target=/work",
            $script:ValhallaImage,
            "bash",
            "-lc",
            $dockerCommand
        )

        if (-not (Test-Path $tileOutput)) {
            throw "Valhalla did not produce tiles at $tileOutput"
        }

        Add-Type -AssemblyName System.IO.Compression.FileSystem
        if (Test-Path $zipPath) {
            Remove-Item -Force $zipPath
        }
        [System.IO.Compression.ZipFile]::CreateFromDirectory($tileOutput, $zipPath, [System.IO.Compression.CompressionLevel]::Optimal, $false)

        if (-not $KeepTiles) {
            Remove-Item -Recurse -Force $tileOutput
        }

        if ((-not $KeepPbf) -and (Test-Path $pbfPath)) {
            Remove-Item -Force $pbfPath
        }
    }

    $sizeMb = Get-LocalZipSizeMb -ZipPath $zipPath
    Write-Log "Created $zipPath ($sizeMb MB)"
    return [pscustomobject]@{ id = $RegionEntry.id; zipPath = $zipPath; fileSizeMb = $sizeMb; configPath = $configPath }
}

function Write-OutputManifest {
    param(
        [object[]]$AllRegions,
        [object[]]$ReleaseManifest
    )

    $releaseLookup = @{}
    foreach ($entry in $ReleaseManifest) {
        $releaseLookup[[string]$entry.id] = [double]$entry.fileSizeMb
    }

    $manifest = foreach ($regionEntry in $AllRegions) {
        $localZipPath = Join-Path $script:ZipDir $regionEntry.fileName
        $localSize = Get-LocalZipSizeMb -ZipPath $localZipPath
        $effectiveSize = $localSize
        if ($null -eq $effectiveSize -and $releaseLookup.ContainsKey($regionEntry.id)) {
            $effectiveSize = $releaseLookup[$regionEntry.id]
        }

        if ($null -ne $effectiveSize) {
            [pscustomobject][ordered]@{
                id = $regionEntry.id
                names = [ordered]@{
                    en = $regionEntry.names.en
                    de = $regionEntry.names.de
                    fr = $regionEntry.names.fr
                    it = $regionEntry.names.it
                }
                fileName = $regionEntry.fileName
                fileSizeMb = [Math]::Round([double]$effectiveSize, 2)
            }
        }
    }

    $json = $manifest | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($script:ManifestPath, $json, [System.Text.UTF8Encoding]::new($false))
    $manifest | Select-Object id, fileName, fileSizeMb | Export-Csv -Path $script:SizesPath -NoTypeInformation
    Write-Log "Wrote manifest: $script:ManifestPath"
    Write-Log "Wrote size report: $script:SizesPath"
}

Write-Log "Loading region metadata from $script:MetadataPath"
$allRegions = @(Get-RegionMetadata)
$releaseManifest = @(Get-ReleaseManifest -Tag $ReleaseTag)
$selectedRegions = @(Get-SelectedRegions -AllRegions $allRegions -ReleaseManifest $releaseManifest -RequestedRegions $Region -MissingOnlyMode $MissingOnly.IsPresent)

if ($selectedRegions.Count -eq 0) {
    Write-Log "No regions selected. Writing merged manifest only."
    Write-OutputManifest -AllRegions $allRegions -ReleaseManifest $releaseManifest
    exit 0
}

Write-Log "Selected regions: $($selectedRegions.id -join ', ')"
Write-Log "Output directory: $script:OutputRoot"
Write-Log "Log file: $script:LogPath"

Invoke-External -FilePath "docker" -ArgumentList @("info")
if ($PullImage) {
    Invoke-External -FilePath "docker" -ArgumentList @("pull", $script:ValhallaImage)
}

$built = @()
foreach ($regionEntry in $selectedRegions) {
    $built += Build-RegionTiles -RegionEntry $regionEntry
}

Write-OutputManifest -AllRegions $allRegions -ReleaseManifest $releaseManifest

Write-Log "Build complete. Generated $($built.Count) region zip(s)."
