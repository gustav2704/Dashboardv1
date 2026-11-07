<#
Exporta evidencia de configuracion MT5 sin modificar ninguna terminal.

Ejemplo en la VPS 145.63.130.53:
  powershell.exe -ExecutionPolicy Bypass -File .\export-mt5-bot-sets.ps1 `
    -TerminalRoot 'C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal'

El resultado es un ZIP en la carpeta indicada por -OutputParent. Incluye:
  sets.csv       todos los .set encontrados, con hash y claves relevantes
  charts.csv     todos los .chr, con el bloque <expert> y objetos coincidentes
  experts.csv    todos los EX5, con hash y ruta relativa
  charts\       copias de los .chr para permitir una lectura forense local
  sets\         copias de los .set, sin mover ni borrar originales

No abre MT5, no reinicia procesos, no toca posiciones/ordenes y solo escribe
en la carpeta de salida.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TerminalRoot,

    [string]$OutputParent = (Join-Path (Get-Location) 'mt5_sets_exports'),

    [string[]]$Needle = @('ULTRADAX_D1', '41500119', 'GDAXI')
)

$ErrorActionPreference = 'Stop'

function Read-TextFile([string]$Path) {
    try {
        $bytes = [IO.File]::ReadAllBytes($Path)
        if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xff -and $bytes[1] -eq 0xfe) {
            return [Text.Encoding]::Unicode.GetString($bytes)
        }
        if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xfe -and $bytes[1] -eq 0xff) {
            return [Text.Encoding]::BigEndianUnicode.GetString($bytes)
        }
        return [Text.Encoding]::UTF8.GetString($bytes)
    } catch {
        return ''
    }
}

function Get-Field([string]$Text, [string[]]$Names) {
    foreach ($name in $Names) {
        $match = [regex]::Match($Text, '(?im)^' + [regex]::Escape($name) + '=(.*)$')
        if ($match.Success) { return $match.Groups[1].Value.Trim() }
    }
    return ''
}

function Get-NeedleHits([string]$Text) {
    $hits = @()
    foreach ($needleValue in $Needle) {
        if (-not [string]::IsNullOrWhiteSpace($needleValue) -and
            $Text.IndexOf($needleValue, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $hits += $needleValue
        }
    }
    return ($hits -join ';')
}

if (-not (Test-Path -LiteralPath $TerminalRoot -PathType Container)) {
    throw "No existe TerminalRoot: $TerminalRoot"
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$exportDir = Join-Path $OutputParent ("mt5_sets_export_{0}" -f $stamp)
$setsDir = Join-Path $exportDir 'sets'
$chartsDir = Join-Path $exportDir 'charts'
New-Item -ItemType Directory -Path $setsDir, $chartsDir -Force | Out-Null

$terminalDirs = @(Get-ChildItem -LiteralPath $TerminalRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'MQL5') -PathType Container })
if ($terminalDirs.Count -eq 0) { throw "No se encontraron DataDirs MT5 debajo de $TerminalRoot" }

$setRows = @()
$chartRows = @()
$expertRows = @()

foreach ($terminal in $terminalDirs) {
    $dataDir = $terminal.FullName
    $terminalId = $terminal.Name
    $mql5 = Join-Path $dataDir 'MQL5'
    $profileRoot = Join-Path $mql5 'Profiles\Charts'
    $terminalSetsDir = Join-Path $setsDir $terminalId
    $terminalChartsDir = Join-Path $chartsDir $terminalId
    New-Item -ItemType Directory -Path $terminalSetsDir, $terminalChartsDir -Force | Out-Null

    $setFiles = @(Get-ChildItem -LiteralPath $mql5 -Recurse -File -Filter '*.set' -ErrorAction SilentlyContinue)
    foreach ($file in $setFiles) {
        $relative = $file.FullName.Substring($dataDir.Length).TrimStart('\')
        $safeRelative = $relative -replace '[\\/:*?"<>|]', '__'
        $destination = Join-Path $terminalSetsDir $safeRelative
        Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
        $text = Read-TextFile $file.FullName
        $setRows += [pscustomobject]@{
            terminal_id = $terminalId
            data_dir = $dataDir
            source_path = $file.FullName
            relative_path = $relative
            exported_path = $destination
            bytes = $file.Length
            sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            expert_path = Get-Field $text @('path', 'Expert', 'expert')
            symbol = Get-Field $text @('symbol', 'Symbol')
            timeframe = Get-Field $text @('period', 'Timeframe', 'timeframe')
            magic = Get-Field $text @('MagicNumber', 'MyNumber', 'InpMagicNumber', 'InpMagic')
            comment = Get-Field $text @('CustomComment', 'OrderComment')
            needle_hits = Get-NeedleHits $text
        }
    }

    $chartFiles = @()
    if (Test-Path -LiteralPath $profileRoot -PathType Container) {
        $chartFiles = @(Get-ChildItem -LiteralPath $profileRoot -Recurse -File -Filter '*.chr' -ErrorAction SilentlyContinue)
    }
    foreach ($file in $chartFiles) {
        $relative = $file.FullName.Substring($dataDir.Length).TrimStart('\')
        $safeRelative = $relative -replace '[\\/:*?"<>|]', '__'
        $destination = Join-Path $terminalChartsDir $safeRelative
        Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
        $text = Read-TextFile $file.FullName
        $expertBlock = ''
        $expertMatch = [regex]::Match($text, '(?is)<expert>(.*?)</expert>')
        if ($expertMatch.Success) { $expertBlock = $expertMatch.Groups[1].Value }
        $objects = @([regex]::Matches($text, '(?is)<object>(.*?)</object>') |
            ForEach-Object { Get-Field $_.Groups[1].Value @('descr', 'name') } |
            Where-Object { $_ -and (Get-NeedleHits $_) })
        $chartRows += [pscustomobject]@{
            terminal_id = $terminalId
            data_dir = $dataDir
            profile = $file.Directory.Name
            source_path = $file.FullName
            relative_path = $relative
            exported_path = $destination
            bytes = $file.Length
            last_write_utc = $file.LastWriteTimeUtc.ToString('o')
            sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            expert_path = Get-Field $expertBlock @('path')
            symbol = Get-Field $text @('symbol')
            timeframe = (Get-Field $text @('period', 'period_size'))
            magic = Get-Field $expertBlock @('MagicNumber', 'MyNumber', 'InpMagicNumber', 'InpMagic')
            comment = Get-Field $expertBlock @('CustomComment', 'OrderComment')
            expert_block_present = [bool]$expertMatch.Success
            needle_hits = Get-NeedleHits $text
            matching_objects = ($objects -join ' | ')
        }
    }

    $expertFiles = @(Get-ChildItem -LiteralPath (Join-Path $mql5 'Experts') -Recurse -File -Filter '*.ex5' -ErrorAction SilentlyContinue)
    foreach ($file in $expertFiles) {
        $relative = $file.FullName.Substring($dataDir.Length).TrimStart('\')
        $expertRows += [pscustomobject]@{
            terminal_id = $terminalId
            data_dir = $dataDir
            source_path = $file.FullName
            relative_path = $relative
            bytes = $file.Length
            sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            filename = $file.Name
            needle_hits = Get-NeedleHits $file.Name
        }
    }
}

$setRows | Export-Csv -LiteralPath (Join-Path $exportDir 'sets.csv') -NoTypeInformation -Encoding UTF8
$chartRows | Export-Csv -LiteralPath (Join-Path $exportDir 'charts.csv') -NoTypeInformation -Encoding UTF8
$expertRows | Export-Csv -LiteralPath (Join-Path $exportDir 'experts.csv') -NoTypeInformation -Encoding UTF8
$needleText = $Needle -join ', '
@(
    'MT5 read-only evidence export'
    ('Created: ' + (Get-Date).ToString('o'))
    ('TerminalRoot: ' + $TerminalRoot)
    ('Needles: ' + $needleText)
    ''
    'No MT5 process was started, stopped, or modified.'
    'The charts.csv column expert_block_present distinguishes an actual EA attachment from chart objects/labels.'
    'The matching_objects column is evidence only and must not be used as EA inputs.'
) | Set-Content -LiteralPath (Join-Path $exportDir 'README.txt') -Encoding UTF8

$zipPath = Join-Path $OutputParent ("mt5_sets_export_{0}.zip" -f $stamp)
Compress-Archive -Path $exportDir -DestinationPath $zipPath -Force
[pscustomobject]@{
    export_directory = $exportDir
    zip = $zipPath
    terminals = $terminalDirs.Count
    sets = $setRows.Count
    charts = $chartRows.Count
    experts = $expertRows.Count
    needle = $needleText
} | ConvertTo-Json -Compress
