import sys
from remote import run_ps

TAG = sys.argv[1] if len(sys.argv) > 1 else "145"

# Enumerate terminal dirs, then all .chr files under each MQL5\Profiles\Charts
script = r"""
$root = '<ROOT>'
Get-ChildItem $root -Directory | ForEach-Object {
  $term = $_.Name
  $chartsRoot = Join-Path $_.FullName 'MQL5\Profiles\Charts'
  if (Test-Path $chartsRoot) {
    Get-ChildItem $chartsRoot -Recurse -Filter *.chr -File -ErrorAction SilentlyContinue | ForEach-Object {
      Write-Output ($term + '|' + $_.FullName.Substring($chartsRoot.Length+1) + '|' + $_.Length)
    }
  }
}
""".replace("<ROOT>", {
    "145": "C:/Users/Administrator/AppData/Roaming/MetaQuotes/Terminal",
    "100": "C:/Users/Administrador/AppData/Roaming/MetaQuotes/Terminal",
}[TAG])

status, out = run_ps(TAG, script)
print("STATUS", status)
print(out)
