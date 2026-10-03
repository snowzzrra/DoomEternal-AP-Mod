[CmdletBinding()]
param(
    [string]$OutputDir = "build\\release\\build\\client",
    [string]$CoreSource = "",
    [switch]$UseCurrentToolchain,
    [switch]$Preflight
)

$ErrorActionPreference = "Stop"

function Invoke-NativeBuild {
    param([string]$RepositoryRoot, [string]$BuildDirectory, [string]$CoreRoot, [switch]$PreflightOnly)

    foreach ($tool in "cl.exe", "link.exe", "dumpbin.exe", "rc.exe") {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            throw "MSVC x64 toolchain is not active: missing $tool. Install Visual Studio 2022 Build Tools with Desktop development with C++."
        }
    }
    if ($PreflightOnly) {
        Write-Output "NATIVE_CLIENT_PREFLIGHT toolchain=msvc-x64 status=PASS"
        return
    }

    if (Test-Path -LiteralPath $BuildDirectory) {
        Remove-Item -LiteralPath $BuildDirectory -Recurse -Force
    }
    $null = New-Item -ItemType Directory -Path $BuildDirectory -Force
    $clientDirectory = Join-Path $RepositoryRoot "native\\client"
    $coreRoot = $CoreRoot
    $compileCxx = @("/nologo", "/std:c++17", "/O2", "/MT", "/EHsc", "/guard:cf", "/D_M_AMD64", "/DNOMINMAX", "/I$RepositoryRoot", "/I$coreRoot\\include", "/I$coreRoot\\src", "/I$coreRoot\\build\\generated", "/c")
    $sources = @(
        "ap_client_exe.cpp", "command_queue.cpp", "ap_client_path_utils.cpp", "game_state_probe.cpp",
        "sentinel_command_client.cpp", "ap_rpc_health_state.cpp", "ammo_hotkey.cpp"
    )
    $objects = @()
    foreach ($source in $sources) {
        $object = Join-Path $BuildDirectory (([IO.Path]::GetFileNameWithoutExtension($source)) + ".obj")
        & cl.exe @compileCxx (Join-Path $clientDirectory $source) "/Fo$object"
        if ($LASTEXITCODE -ne 0) { throw "MSVC failed compiling $source" }
        $objects += $object
    }
    foreach ($source in @("inspection_client.cpp", "protocol.cpp", "commands.cpp")) {
        $object = Join-Path $BuildDirectory (([IO.Path]::GetFileNameWithoutExtension($source)) + ".obj")
        & cl.exe @compileCxx (Join-Path $coreRoot "src\\$source") "/Fo$object"
        if ($LASTEXITCODE -ne 0) { throw "MSVC failed compiling $source" }
        $objects += $object
    }

    $resourceObject = Join-Path $BuildDirectory "ap_client.res"
    & rc.exe /nologo "/fo$resourceObject" (Join-Path $clientDirectory "ap_client.rc")
    if ($LASTEXITCODE -ne 0) { throw "Resource compiler failed creating ap_client.res" }

    $clientOutput = Join-Path $BuildDirectory "ap_client.exe"
    & link.exe /nologo /DYNAMICBASE /NXCOMPAT /HIGHENTROPYVA /GUARD:CF "/OUT:$clientOutput" @objects $resourceObject bcrypt.lib version.lib user32.lib
    if ($LASTEXITCODE -ne 0) { throw "MSVC linker failed creating ap_client.exe" }

    $probeOutput = Join-Path $BuildDirectory "save_death_probe.exe"
    $probeObject = Join-Path $BuildDirectory "save_death_probe.obj"
    & cl.exe /nologo /std:c++17 /O2 /MT /EHsc (Join-Path $RepositoryRoot "native\\probes\\save_death_probe.cpp") "/Fe$probeOutput" "/Fo$probeObject"
    if ($LASTEXITCODE -ne 0) { throw "MSVC failed creating save_death_probe.exe" }

    foreach ($output in $clientOutput, $probeOutput) {
        $bytes = [IO.File]::ReadAllBytes($output)
        if ($bytes.Length -lt 2 -or $bytes[0] -ne 0x4d -or $bytes[1] -ne 0x5a) {
            throw "Native build produced an invalid PE file: $output"
        }
    }
    $headers = & dumpbin.exe /headers $clientOutput
    $headersExitCode = $LASTEXITCODE
    if ($headersExitCode -ne 0) { throw "dumpbin /headers failed with exit code $headersExitCode" }
    $isX64 = [bool]($headers | Select-String -Pattern "machine \(x64\)" -Quiet)
    if (-not $isX64) { throw "ap_client.exe is not an x64 PE binary" }

    $imports = & dumpbin.exe /imports $clientOutput
    $importsExitCode = $LASTEXITCODE
    if ($importsExitCode -ne 0) { throw "dumpbin /imports failed with exit code $importsExitCode" }
    $importsRpcrt4 = [bool]($imports | Select-String -Pattern "RPCRT4.dll" -Quiet)
    if ($importsRpcrt4) { throw "Typed Core client must not import RPCRT4" }
    $signingRequired = $env:DOOMEAP_SIGNING_REQUIRED -in @("1", "true", "TRUE", "yes", "YES")
    $signingThumbprint = ($env:DOOMEAP_SIGNING_CERT_SHA1 -replace '\s', '')
    if ($signingRequired -and -not $signingThumbprint) {
        throw "Authenticode signing was required but DOOMEAP_SIGNING_CERT_SHA1 is empty"
    }
    if ($signingThumbprint) {
        $signTool = Get-Command signtool.exe -ErrorAction SilentlyContinue
        if (-not $signTool) { throw "Authenticode signing was requested but signtool.exe is unavailable" }
        $timestampUrl = if ($env:DOOMEAP_SIGNING_TIMESTAMP_URL) { $env:DOOMEAP_SIGNING_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
        & $signTool.Source sign /sha1 $signingThumbprint /fd SHA256 /tr $timestampUrl /td SHA256 $clientOutput
        if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed with exit code $LASTEXITCODE" }
        & $signTool.Source verify /pa /v $clientOutput
        if ($LASTEXITCODE -ne 0) { throw "Authenticode verification failed with exit code $LASTEXITCODE" }
        Write-Output "NATIVE_CLIENT_SIGNING status=verified"
    } else {
        Write-Output "NATIVE_CLIENT_SIGNING status=unsigned-local reputation=unestablished"
    }

    $defender = Get-Command MpCmdRun.exe -ErrorAction SilentlyContinue
    $defenderPath = if ($defender) { $defender.Source } else { $null }
    if (-not $defender) {
        $candidateDefenderPath = Join-Path $env:ProgramFiles "Windows Defender\\MpCmdRun.exe"
        if (Test-Path -LiteralPath $candidateDefenderPath -PathType Leaf) { $defenderPath = $candidateDefenderPath }
    }
    if ($defenderPath) {
        & $defenderPath -Scan -ScanType 3 -File $clientOutput
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $clientOutput -PathType Leaf)) {
            throw "Microsoft Defender did not clear ap_client.exe (exit code $LASTEXITCODE)"
        }
        Write-Output "NATIVE_CLIENT_DEFENDER status=clear"
    } else {
        Write-Output "NATIVE_CLIENT_DEFENDER status=unavailable"
    }

    $executionProbe = Join-Path $BuildDirectory "__missing_execution_probe__"
    $probeProcess = Start-Process -FilePath $clientOutput -ArgumentList $executionProbe -WindowStyle Hidden -Wait -PassThru
    if ($probeProcess.ExitCode -ne 1) { throw "ap_client.exe execution probe returned $($probeProcess.ExitCode), expected 1" }
    $hashStream = [IO.File]::OpenRead($clientOutput)
    try {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try { $frozenHash = ([BitConverter]::ToString($sha256.ComputeHash($hashStream))).Replace("-", "").ToLowerInvariant() }
        finally { $sha256.Dispose() }
    } finally {
        $hashStream.Dispose()
    }
    Write-Output "NATIVE_CLIENT_FINAL sha256=$frozenHash"
    Write-Output "NATIVE_CLIENT windows-msvc output=$BuildDirectory"
}

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path
if (-not $CoreSource) { throw "Pass -CoreSource with the configured Sentinel Core source directory" }
$coreDirectory = (Resolve-Path -LiteralPath $CoreSource).Path
if (-not (Test-Path -LiteralPath (Join-Path $coreDirectory "build\\generated\\sentinel_version.h"))) {
    throw "Configure Sentinel Core before building its client"
}
$releaseRoot = [IO.Path]::GetFullPath((Join-Path $repositoryRoot "build\\release"))
$buildDirectory = if ([IO.Path]::IsPathRooted($OutputDir)) {
    [IO.Path]::GetFullPath($OutputDir)
} else {
    [IO.Path]::GetFullPath((Join-Path $repositoryRoot $OutputDir))
}
if (-not $buildDirectory.StartsWith($releaseRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Native build output must remain under $releaseRoot"
}

if (-not $UseCurrentToolchain) {
    $vswhere = @(
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\\Installer\\vswhere.exe"),
        (Join-Path $env:ProgramFiles "Microsoft Visual Studio\\Installer\\vswhere.exe")
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $vswhere) {
        throw "Visual Studio Build Tools were not found. Install Visual Studio 2022 Build Tools with Desktop development with C++."
    }
    $installation = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0 -or -not $installation) {
        throw "Visual Studio Build Tools x64 components were not found. Install the Desktop development with C++ workload."
    }
    $installation = $installation.Trim()
    $developerCommand = Join-Path $installation "Common7\\Tools\\VsDevCmd.bat"
    if (-not $installation -or -not (Test-Path -LiteralPath $developerCommand)) {
        throw "Visual Studio Build Tools x64 components were not found. Install the Desktop development with C++ workload."
    }
    $preflightArgument = if ($Preflight) { " -Preflight" } else { "" }
    $command = 'call "{0}" -arch=x64 -host_arch=x64 >nul && powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{1}" -OutputDir "{2}" -CoreSource "{4}" -UseCurrentToolchain{3}' -f $developerCommand, $PSCommandPath, $buildDirectory, $preflightArgument, $coreDirectory
    & cmd.exe /d /s /c $command
    if ($LASTEXITCODE -ne 0) { throw "Native Windows build failed with exit code $LASTEXITCODE" }
    exit 0
}

Invoke-NativeBuild -RepositoryRoot $repositoryRoot -BuildDirectory $buildDirectory -CoreRoot $coreDirectory -PreflightOnly:$Preflight
