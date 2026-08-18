$root = 'C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal'
$dirs = @('1D8B0F5DE56F27A2A76D83BE69DE515E','6C3C6A11D1C3791DD4DBF45421BF8028','6FBEE76C719DC78AB2AE839B5A0C7442','951377C0D4197CD328B0F10CBC4A6039')
foreach ($d in $dirs) {
  $pc = Join-Path (Join-Path (Join-Path $root $d) 'MQL5\Profiles') 'Charts'
  Write-Output "===== $d ====="
  if (Test-Path $pc) {
    Get-ChildItem $pc -Directory -ErrorAction SilentlyContinue | ForEach-Object {
      $n = (Get-ChildItem $_.FullName -Filter *.chr -File -ErrorAction SilentlyContinue).Count
      Write-Output ("  PROFILE=" + $_.Name + "  charts=" + $n)
    }
  } else { Write-Output '  <no Profiles\Charts>' }
}
