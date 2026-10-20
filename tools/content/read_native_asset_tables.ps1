param([Parameter(Mandatory=$true)][string]$GameBase,
      [Parameter(Mandatory=$true)][string]$InputFile,
      [Parameter(Mandatory=$true)][string]$OutputFile)
$ErrorActionPreference = 'Stop'
$resolve = [ResolveEventHandler] {
    param($sender, $eventArgs)
    $path = Join-Path $GameBase (([Reflection.AssemblyName]::new($eventArgs.Name).Name) + '.dll')
    if (Test-Path -LiteralPath $path) { return [Reflection.Assembly]::LoadFrom($path) }
    return $null
}
[AppDomain]::CurrentDomain.add_AssemblyResolve($resolve)
try {
    $assembly = [Reflection.Assembly]::LoadFrom((Join-Path $GameBase 'DEternal_loadMods.exe'))
    $flags = [Reflection.BindingFlags]'Public,NonPublic,Static,Instance'
    $parse = $assembly.GetType('EternalModLoader.Mods.Resources.MapResources.MapResourcesFile', $true).GetMethod('Parse', $flags)
    $inputs = [IO.File]::ReadAllText($InputFile) | ConvertFrom-Json
    $tables = [ordered]@{}
    foreach ($property in $inputs.PSObject.Properties) {
        $bytes = [IO.File]::ReadAllBytes($property.Value.path)
        $document = $parse.Invoke($null, [object[]]@(,$bytes))
        $tables[$property.Name] = @($document.Assets | ForEach-Object {
            [ordered]@{mapResourceType=$document.AssetTypes[$_.AssetTypeIndex]; name=$_.Name}
        })
    }
    $tables | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $OutputFile -Encoding UTF8
} finally {
    [AppDomain]::CurrentDomain.remove_AssemblyResolve($resolve)
}
