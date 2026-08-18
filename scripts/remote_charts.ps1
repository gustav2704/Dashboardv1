$root = 'C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal'
$dirs = @('1D8B0F5DE56F27A2A76D83BE69DE515E','6C3C6A11D1C3791DD4DBF45421BF8028','6FBEE76C719DC78AB2AE839B5A0C7442','951377C0D4197CD328B0F10CBC4A6039')
foreach ($d in $dirs) {
  $charts = Join-Path (Join-Path $root $d) 'Profiles\Charts'
  Write-Output "=== $d ==="
  if (Test-Path $charts) {
    $n=0
    Get-ChildItem $charts -File -Filter *.chr -ErrorAction SilentlyContinue | ForEach-Object { $n++; Write-Output ("  " + $_.Name + "  " + $_.Length) }
    Write-Output "  COUNT=$n"
  } else { Write-Output '  <no Charts dir>' }
}
