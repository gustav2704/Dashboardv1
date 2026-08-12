param(
    [Parameter(Mandatory=$false)]
    [string]$DataDir = 'C:\Users\Admin\AppData\Roaming\MetaQuotes\Terminal\91667A77170349F91845E5FC96BD9A8C',
    [switch]$InstallBotInventory
)
$ErrorActionPreference = 'Stop'
$DataDir = (Resolve-Path -LiteralPath $DataDir).Path
$origin = Join-Path $DataDir 'origin.txt'
if (-not (Test-Path -LiteralPath $origin)) { throw "DataDir no contiene origin.txt: $DataDir" }
$InstallDir = (Get-Content -LiteralPath $origin -TotalCount 1).Trim()
$editor = Join-Path $InstallDir 'MetaEditor64.exe'
if (-not (Test-Path -LiteralPath $editor)) { throw "No se encontró MetaEditor64.exe: $editor" }
$source = Join-Path $PSScriptRoot 'mql5\DashboardBridge.mq5'
$destinationDir = Join-Path $DataDir 'MQL5\Services\Dashboardv1'
$destination = Join-Path $destinationDir 'DashboardBridge.mq5'
$log = Join-Path $PSScriptRoot 'DashboardBridge.compile.log'
New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $destination -Force
$process = Start-Process -FilePath $editor -ArgumentList @("/compile:$destination", "/log:$log") -Wait -PassThru -WindowStyle Hidden
if (-not (Test-Path -LiteralPath $log)) { throw "MetaEditor no creó el log de compilación (exit $($process.ExitCode))" }
$content = Get-Content -LiteralPath $log -Raw
if ($content -notmatch '0 errors?, 0 warnings?') { throw "Compilación MQL5 no limpia. Revisa: $log`n$content" }
$ex5 = [IO.Path]::ChangeExtension($destination, '.ex5')
if (-not (Test-Path -LiteralPath $ex5)) { throw "No se generó EX5: $ex5" }
New-Item -ItemType Directory -Path (Join-Path $DataDir 'MQL5\Files\Dashboardv1\Requests') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $DataDir 'MQL5\Files\Dashboardv1\Responses\Archive') -Force | Out-Null
$inventory = $null
if ($InstallBotInventory) {
    $inventorySource = Join-Path $PSScriptRoot 'mql5\BotInventoryReport.mq5'
    $inventoryDir = Join-Path $DataDir 'MQL5\Scripts\Dashboardv1'
    $inventoryTarget = Join-Path $inventoryDir 'BotInventoryReport.mq5'
    $inventoryLog = Join-Path $PSScriptRoot 'BotInventoryReport.compile.log'
    New-Item -ItemType Directory -Path $inventoryDir -Force | Out-Null
    Copy-Item -LiteralPath $inventorySource -Destination $inventoryTarget -Force
    $catalogDir = Join-Path $DataDir 'MQL5\Files\BotInventoryReport'
    New-Item -ItemType Directory -Path $catalogDir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'mql5\BotInventoryCatalog.csv') -Destination (Join-Path $catalogDir 'BotInventoryCatalog.csv') -Force
    $process = Start-Process -FilePath $editor -ArgumentList @("/compile:$inventoryTarget", "/log:$inventoryLog") -Wait -PassThru -WindowStyle Hidden
    if (-not (Test-Path -LiteralPath $inventoryLog)) { throw "MetaEditor did not create the inventory compile log (exit $($process.ExitCode))" }
    $content = Get-Content -LiteralPath $inventoryLog -Raw
    if ($content -notmatch '0 errors?, 0 warnings?') { throw "Inventory compilation is not clean. Review: $inventoryLog`n$content" }
    $inventoryEx5 = [IO.Path]::ChangeExtension($inventoryTarget, '.ex5')
    if (-not (Test-Path -LiteralPath $inventoryEx5)) { throw "Inventory EX5 was not produced: $inventoryEx5" }
    $inventory = [PSCustomObject]@{ Source=$inventoryTarget; Ex5=$inventoryEx5; CompileLog=$inventoryLog }
}
[PSCustomObject]@{ DataDir=$DataDir; InstallDir=$InstallDir; Source=$destination; Ex5=$ex5; CompileLog=$log; Inventory=$inventory; Status='compiled' }
