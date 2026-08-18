$root = 'C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal'
$dirs = @('6C3C6A11D1C3791DD4DBF45421BF8028','6FBEE76C719DC78AB2AE839B5A0C7442')
foreach ($d in $dirs) {
  $p = Join-Path $root $d
  Write-Output "=== $d Profiles tree (top 2 levels) ==="
  if (Test-Path (Join-Path $p 'Profiles')) {
    Get-ChildItem (Join-Path $p 'Profiles') -Recurse -Directory -ErrorAction SilentlyContinue | ForEach-Object { Write-Output $_.FullName }
  } else { Write-Output '  <no Profiles>' }
  Write-Output "=== $d *.chr files (up to 40) ==="
  Get-ChildItem $p -Recurse -Filter *.chr -File -ErrorAction SilentlyContinue | Select-Object -First 40 | ForEach-Object { Write-Output ("  " + $_.FullName + "  " + $_.Length) }
}
