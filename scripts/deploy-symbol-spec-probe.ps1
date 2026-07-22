param(
    [ValidateSet('FPM','DarwinexCustom','DarwinexLive')]
    [string]$Profile = 'FPM'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$profiles = @{
  FPM = @{ DataDir='C:\Users\Admin\AppData\Roaming\MetaQuotes\Terminal\20ADDFAD12B439FB6F764A62C82D6A6E'; Symbols=@('US100','GER40','US30','XAUUSD.cyr','GBPAUD_M1','US500','GDAXI_M1','WS30','WS30_DK_2016','XAUUSD_M1') }
  DarwinexCustom = @{ DataDir='D:\TerminalesMT5\Darwinex Strategy Tester'; Symbols=@('NDX_M1','GDAXI_M1','WS30_DK_2016','XAUUSD_M1','GBPAUD','SP500_tick_2025_26jun') }
  DarwinexLive = @{ DataDir='C:\Users\Admin\AppData\Roaming\MetaQuotes\Terminal\9265798B599F649497F9A17BEC9C76C6'; Symbols=@('NAS100','GER40','US30','XAUUSD','GBPAUD','SPX500') }
}
$target=$profiles[$Profile]; $dataDir=(Resolve-Path -LiteralPath $target.DataDir).Path
$origin=Join-Path $dataDir 'origin.txt'
$installDir=if(Test-Path -LiteralPath $origin){(Get-Content -LiteralPath $origin -TotalCount 1).Trim()}else{$dataDir}
$editor=Join-Path $installDir 'MetaEditor64.exe'; if(-not (Test-Path $editor)){throw "MetaEditor missing: $editor"}
$scriptDir=Join-Path $dataDir 'MQL5\Scripts\ContractRiskAudit'; $filesDir=Join-Path $dataDir 'MQL5\Files\ContractRiskAudit'
New-Item -ItemType Directory -Path $scriptDir,$filesDir -Force | Out-Null
$source=Join-Path $root 'mql5\SymbolSpecProbe.mq5'; $destination=Join-Path $scriptDir 'SymbolSpecProbe.mq5'
Copy-Item -LiteralPath $source -Destination $destination -Force
$config=Join-Path $filesDir 'SymbolSpecProbeConfig.csv'
@('source,symbol') + ($target.Symbols | ForEach-Object { "$Profile,$_" }) | Set-Content -LiteralPath $config -Encoding ascii
$log=Join-Path $root "SymbolSpecProbe-$Profile.compile.log"
$p=Start-Process -FilePath $editor -ArgumentList @("/compile:$destination", "/log:$log") -Wait -PassThru -WindowStyle Hidden
if(-not (Test-Path $log) -or (Get-Content $log -Raw) -notmatch '0 errors?, 0 warnings?'){throw "Compilation failed; see $log"}
[PSCustomObject]@{Profile=$Profile; Script=(Join-Path $scriptDir 'SymbolSpecProbe.ex5'); Config=$config; Output=(Join-Path $filesDir "symbol_specs_$Profile.json"); CompileLog=$log; NextStep='In that terminal drag Scripts/ContractRiskAudit/SymbolSpecProbe onto any chart, then run the HTML builder.'}
