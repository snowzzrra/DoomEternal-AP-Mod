[CmdletBinding()]
param(
    [switch]$Preflight,
    [switch]$SkipValidation,
    [switch]$RebuildRoomResources,
    [switch]$PublishRoomResources,
    [string]$StagedMod,
    [string]$MapSources,
    [string]$Compressor,
    [string]$Python,
    [string]$PipelineReceipt
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$workspace = Split-Path -Parent $repoRoot
$archipelago = Join-Path $workspace "Archipelago"

function Resolve-CanonicalPython {
    $candidates = @()
    if ($Python) { $candidates += $Python }
    if ($env:ARCHIPELAGO_PYTHON) { $candidates += $env:ARCHIPELAGO_PYTHON }
    $candidates += @(
        (Join-Path $workspace ".venv-windows\Scripts\python.exe"),
        (Join-Path $archipelago ".venv-windows\Scripts\python.exe"),
        (Join-Path $repoRoot ".venv-windows\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $resolved = (Resolve-Path -LiteralPath $candidate).Path
            if ($resolved -match '[\\/]bin[\\/]python(3)?$') { continue }
            & $resolved -c "import os,sys; assert os.name == 'nt'; print(sys.executable)" | Out-Null
            if ($LASTEXITCODE -eq 0) { return $resolved }
        }
    }
    foreach ($name in "py.exe", "python.exe") {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $resolved = if ($name -eq "py.exe") {
                (& $command.Source -3 -c "import sys; print(sys.executable)").Trim()
            } else { $command.Source }
            if ($LASTEXITCODE -eq 0 -and $resolved) { return $resolved }
        }
    }
    throw "No canonical Windows Python found. Create ..\.venv-windows or set ARCHIPELAGO_PYTHON to python.exe."
}

function Invoke-Python([string[]]$Arguments) {
    & $script:pythonExe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed ($LASTEXITCODE): $($Arguments -join ' ')" }
}

if (-not (Test-Path -LiteralPath (Join-Path $archipelago "Launcher.py"))) {
    throw "Archipelago checkout missing at $archipelago"
}
$pythonExe = Resolve-CanonicalPython
Write-Output "PYTHON canonical=$pythonExe"
Push-Location $repoRoot
try {
    Invoke-Python @("-c", "import yaml, bsdiff4, PyInstaller, PySide6; print('PYTHON_DEPENDENCIES ok')")
    Invoke-Python @("-m", "tools.release.ci_preflight", "--archipelago-source", $archipelago, "--repo-root", $repoRoot)
    Invoke-Python @("-m", "tools.release.prebuilt_room_resources", "--check", "--repo-root", $repoRoot)
    & (Join-Path $PSScriptRoot "client_windows.ps1") -Preflight
    if ($LASTEXITCODE -ne 0) { throw "MSVC preflight failed" }
    if ($Preflight) {
        Write-Output "WINDOWS_PREFLIGHT status=PASS"
        exit 0
    }

    if ($RebuildRoomResources) {
        if (-not $StagedMod) { throw "-RebuildRoomResources requires -StagedMod (prepared authorial mod tree)" }
        if (-not $MapSources) { $MapSources = Join-Path $repoRoot "data\map_sources.json" }
        if (-not $Compressor) { $Compressor = Join-Path $workspace "Tools\idFileDeCompressor" }
        $roomOutput = Join-Path $repoRoot "build\room-resources"
        Invoke-Python @("-m", "tools.release.build_room_resources", "--repo-root", $repoRoot,
            "--staged-mod", $StagedMod, "--work-dir", (Join-Path $repoRoot "build\room-work"),
            "--compressor", $Compressor, "--map-sources", $MapSources, "--output-dir", $roomOutput,
            "--cache-root", (Join-Path $workspace ".cache\doomeap\room-payloads"))
        if ($PublishRoomResources) {
            $modSha = (& git -c "safe.directory=$repoRoot" -C $repoRoot rev-parse HEAD).Trim()
            $apSha = (& git -c "safe.directory=$archipelago" -C $archipelago rev-parse HEAD).Trim()
            Invoke-Python @("-m", "tools.release.prebuilt_room_resources", "--publish-from", $roomOutput,
                "--repo-root", $repoRoot, "--version", "0.5.2", "--mod-commit", $modSha,
                "--apworld-commit", $apSha)
        }
        Write-Output "ROOM_RESOURCE_MAINTENANCE status=PASS output=$roomOutput published=$PublishRoomResources"
        exit 0
    }

    if (-not $SkipValidation) {
        Invoke-Python @("-m", "tools.validation.pipeline", "full")
    }

    $releaseRoot = Join-Path $repoRoot "build\release"
    $handoff = Join-Path $releaseRoot "handoff"
    if (Test-Path -LiteralPath $handoff) { Remove-Item -LiteralPath $handoff -Recurse -Force }
    $resources = Join-Path $handoff "shared\resources"
    $clientBuild = Join-Path $releaseRoot "build\client"
    $clientHandoff = Join-Path $handoff "shared\client"
    $launcherHandoff = Join-Path $handoff "windows"
    New-Item -ItemType Directory -Force $resources, $clientHandoff, $launcherHandoff | Out-Null

    Invoke-Python @("-m", "tools.release.prebuilt_room_resources", "--export-dir", $resources, "--repo-root", $repoRoot)
    Invoke-Python @("-m", "tools.release.apworld_cache", "--output", (Join-Path $handoff "shared\doometernal.apworld"), "--archipelago-source", $archipelago, "--archipelago-python", $pythonExe)
    & (Join-Path $PSScriptRoot "client_windows.ps1") -OutputDir $clientBuild
    if ($LASTEXITCODE -ne 0) { throw "Native client build failed" }
    Copy-Item -LiteralPath (Join-Path $clientBuild "ap_client.exe") -Destination $clientHandoff -Force
    Copy-Item -LiteralPath (Join-Path $clientBuild "save_death_probe.exe") -Destination $clientHandoff -Force
    Invoke-Python @("-m", "tools.release.audit_binary", "--binary", (Join-Path $clientBuild "ap_client.exe"), "--required", "0.5.2", "--forbid", "v0.3.8-alpha", "--forbid", "v0.3.9-alpha")
    Invoke-Python @("-m", "tools.release.build_launcher", "--output-dir", $launcherHandoff, "--archipelago-source", $archipelago)
    $launcher = Join-Path $launcherHandoff "DoomEternalArchipelagoLauncher.exe"
    & $launcher --self-test
    if ($LASTEXITCODE -ne 0) { throw "Frozen Windows launcher self-test failed" }

    $modSha = (& git -c "safe.directory=$repoRoot" -C $repoRoot rev-parse HEAD).Trim()
    $apSha = (& git -c "safe.directory=$archipelago" -C $archipelago rev-parse HEAD).Trim()
    Invoke-Python @("-m", "tools.release.handoff", "--root", $handoff, "--version", "v0.5.2", "--mod-sha", $modSha, "--apworld-sha", $apSha, "--platform", "windows")
    Invoke-Python @((Join-Path $repoRoot "scripts\release\assemble_ci_artifact.py"), "--handoff", $handoff, "--room-resources-dir", $resources, "--platform", "windows", "--version", "v0.5.2", "--repo-root", $repoRoot, "--output-dir", (Join-Path $repoRoot "build\final-release"))
    Write-Output "WINDOWS_RELEASE status=PASS output=$(Join-Path $repoRoot 'build\final-release')"
} finally {
    Pop-Location
}
