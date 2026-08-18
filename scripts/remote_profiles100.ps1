$root = 'C:\Users\Administrador\AppData\Roaming\MetaQuotes\Terminal'
$dirs = @('13BD90F6BE9FAA167ECD83CA4C4F9A5D','169BA68A25A48540472B316FB50A25B8','455ED109B802E41AC490CBECDE40FE93','6FBEE76C719DC78AB2AE839B5A0C7442','7A0597A353A7F7B1759AAEABEFBC896E','7B992C9F4C8405A86F0774DD4193BFFE','81A933A9AFC5DE3C23B15CAB19C63850','C59B2EBA0DCBEDCCBFE7CA7E011A3F88','CC3A30E70B6FDF563DFA143DA3E22AD5')
foreach ($d in $dirs) {
  $p = Join-Path $root $d
  $origin = Join-Path $p 'origin.txt'
  Write-Output "===== $d ====="
  if (Test-Path $origin) { Write-Output ("ORIGIN: " + (Get-Content $origin -Raw -ErrorAction SilentlyContinue)) }
  else { Write-Output 'ORIGIN: <none>' }
  $pc = Join-Path (Join-Path (Join-Path $p 'MQL5\Profiles') 'Charts')
  if (Test-Path $pc) {
    Get-ChildItem $pc -Directory -ErrorAction SilentlyContinue | ForEach-Object {
      $n = (Get-ChildItem $_.FullName -Filter *.chr -File -ErrorAction SilentlyContinue).Count
      Write-Output ("  PROFILE=" + $_.Name + "  charts=" + $n)
    }
  } else { Write-Output '  <no Profiles\Charts>' }
}
