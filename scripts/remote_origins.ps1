$root = 'C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal'
$dirs = @('1D8B0F5DE56F27A2A76D83BE69DE515E','2F12A47F908C38E1243546707DA9A3E6','6C3C6A11D1C3791DD4DBF45421BF8028','6CBF7564561DE9AB10D5A7C696F37CDC','6FBEE76C719DC78AB2AE839B5A0C7442','951377C0D4197CD328B0F10CBC4A6039','9DE44EFC77A6A3ABBD348AFCCFB49D70','D0E8209F77C8CF37AD8BF550E51FF075','E67EB8BA6BADCDA78B527BBA0B498F08','F7DD25AEFC111D2D42DEEEC6EAD50771')
foreach ($d in $dirs) {
  $p = Join-Path $root $d
  $origin = Join-Path $p 'origin.txt'
  $hasBridge = Test-Path (Join-Path $p 'MQL5\Files\Dashboardv1')
  $bridgeFiles = ''
  if ($hasBridge) {
    $bridgeFiles = (Get-ChildItem -Recurse (Join-Path $p 'MQL5\Files\Dashboardv1') -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName) -join '; '
  }
  Write-Output "=== $d ==="
  if (Test-Path $origin) { Write-Output ("ORIGIN: " + (Get-Content $origin -Raw -ErrorAction SilentlyContinue)) }
  else { Write-Output 'ORIGIN: <none>' }
  Write-Output "BRIDGE: $hasBridge"
  if ($bridgeFiles) { Write-Output "BRIDGEFILES: $bridgeFiles" }
}
