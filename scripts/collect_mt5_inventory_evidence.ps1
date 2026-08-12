[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$DataDir,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$InventoryFile,

    [string]$OutputZip
)

$ErrorActionPreference = 'Stop'

function Get-SafeLeafName([string]$Name) {
    foreach ($character in [IO.Path]::GetInvalidFileNameChars()) {
        $Name = $Name.Replace([string]$character, '_')
    }
    return $Name
}

function Copy-EvidenceFile([string]$Source, [string]$SourceRelativePath, [string]$ArchiveRelativePath, [string]$DestinationRoot, [System.Collections.Generic.List[object]]$Manifest) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { return }
    $destination = Join-Path $DestinationRoot $ArchiveRelativePath
    $parent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $destination -Force
    $Manifest.Add([PSCustomObject]@{
        source_relative_path = $SourceRelativePath
        archive_path         = $ArchiveRelativePath
        bytes                = (Get-Item -LiteralPath $Source).Length
        sha256               = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash
    })
}

$dataRoot = (Resolve-Path -LiteralPath $DataDir).Path
$inventoryPath = (Resolve-Path -LiteralPath $InventoryFile).Path
$mql5Root = Join-Path $dataRoot 'MQL5'
if (-not (Test-Path -LiteralPath (Join-Path $mql5Root 'Experts') -PathType Container)) {
    throw "DataDir no parece una carpeta de datos MT5: falta $mql5Root\Experts"
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$outputBase = if ($OutputZip) { [IO.Path]::ChangeExtension((Resolve-Path -LiteralPath (Split-Path -Parent $OutputZip)).Path + '\' + (Split-Path -Leaf $OutputZip), $null) } else { Join-Path (Split-Path -Parent $inventoryPath) "MT5InventoryEvidence_$stamp" }
if ($OutputZip) { $zipPath = [IO.Path]::GetFullPath($OutputZip) } else { $zipPath = "$outputBase.zip" }
$stagingRoot = "$outputBase.staging"
if (Test-Path -LiteralPath $stagingRoot) { Remove-Item -LiteralPath $stagingRoot -Recurse -Force }
New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null

$manifest = [System.Collections.Generic.List[object]]::new()
Copy-EvidenceFile $inventoryPath 'inventory\main.csv' 'inventory\main.csv' $stagingRoot $manifest

$inventoryRows = Import-Csv -LiteralPath $inventoryPath
$unknownNames = $inventoryRows |
    Where-Object { $_.attached -eq 'SI' -and $_.trade_status -eq 'MN_DESCONOCIDO' -and $_.ex5_name -and $_.ex5_name -ne '.ex5' } |
    ForEach-Object { [IO.Path]::GetFileNameWithoutExtension($_.ex5_name).ToLowerInvariant() } |
    Sort-Object -Unique

$inventoryFolder = Split-Path -Parent $inventoryPath
Get-ChildItem -LiteralPath $inventoryFolder -File -Filter 'BotInventory_*.csv' -ErrorAction SilentlyContinue |
    ForEach-Object { Copy-EvidenceFile $_.FullName (Join-Path 'inventory' $_.Name) (Join-Path 'inventory' $_.Name) $stagingRoot $manifest }

$expertRoot = Join-Path $mql5Root 'Experts'
$expertFiles = Get-ChildItem -LiteralPath $expertRoot -Recurse -File -Include '*.mq5','*.mqh','*.ex5' -ErrorAction SilentlyContinue |
    Where-Object { $unknownNames -contains [IO.Path]::GetFileNameWithoutExtension($_.Name).ToLowerInvariant() }
foreach ($file in @($expertFiles)) {
    $relative = $file.FullName.Substring($dataRoot.Length).TrimStart('\')
    $archiveName = "experts\$($manifest.Count)_$(Get-SafeLeafName $file.Name)"
    Copy-EvidenceFile $file.FullName $relative $archiveName $stagingRoot $manifest
}

$matchingSets = Get-ChildItem -LiteralPath $mql5Root -Recurse -File -Filter '*.set' -ErrorAction SilentlyContinue |
    Where-Object { $unknownNames -contains [IO.Path]::GetFileNameWithoutExtension($_.Name).ToLowerInvariant() }
foreach ($file in @($matchingSets)) {
    $relative = $file.FullName.Substring($dataRoot.Length).TrimStart('\')
    $archiveName = "sets\$($manifest.Count)_$(Get-SafeLeafName $file.Name)"
    Copy-EvidenceFile $file.FullName $relative $archiveName $stagingRoot $manifest
}

# Chart profiles and experts.dat can hold the active configuration in MT5's
# binary format. They are collected for automated analysis, not edited.
$profilesRoot = Join-Path $mql5Root 'Profiles\Charts'
if (Test-Path -LiteralPath $profilesRoot -PathType Container) {
    Get-ChildItem -LiteralPath $profilesRoot -Recurse -File -Filter '*.chr' -ErrorAction SilentlyContinue |
        ForEach-Object {
            $relative = $_.FullName.Substring($dataRoot.Length).TrimStart('\')
            $archiveName = "chart_profiles\$($manifest.Count)_$(Get-SafeLeafName $_.Name)"
            Copy-EvidenceFile $_.FullName $relative $archiveName $stagingRoot $manifest
        }
}
Copy-EvidenceFile (Join-Path $mql5Root 'experts.dat') 'MQL5\experts.dat' 'MQL5\experts.dat' $stagingRoot $manifest

$manifestPath = Join-Path $stagingRoot 'manifest.csv'
$manifest | Export-Csv -LiteralPath $manifestPath -NoTypeInformation -Encoding utf8
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
Compress-Archive -Path (Join-Path $stagingRoot '*') -DestinationPath $zipPath -Force
Remove-Item -LiteralPath $stagingRoot -Recurse -Force

Write-Output "Paquete creado: $zipPath"
Write-Output "EAs sin Magic incluidos: $($unknownNames.Count)"
Write-Output "Archivos de evidencia: $($manifest.Count)"
