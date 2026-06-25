param(
    [Parameter(Mandatory=$false)]
    [string]$DataDir = 'C:\Users\Admin\AppData\Roaming\MetaQuotes\Terminal\20ADDFAD12B439FB6F764A62C82D6A6E'
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
[PSCustomObject]@{ DataDir=$DataDir; InstallDir=$InstallDir; Source=$destination; Ex5=$ex5; CompileLog=$log; Status='compiled' }

