import os, sys
from remote import run_ps, sftp_download_many

TAG = sys.argv[1] if len(sys.argv) > 1 else "145"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "chr_all", TAG)
os.makedirs(OUT, exist_ok=True)

ROOT = {
    "145": "C:/Users/Administrator/AppData/Roaming/MetaQuotes/Terminal",
    "100": "C:/Users/Administrador/AppData/Roaming/MetaQuotes/Terminal",
}[TAG]

script = r"""
$root = '<ROOT>'
Get-ChildItem $root -Directory | ForEach-Object {
  $term = $_.Name
  $chartsRoot = Join-Path $_.FullName 'MQL5\Profiles\Charts'
  if (Test-Path $chartsRoot) {
    Get-ChildItem $chartsRoot -Recurse -Filter *.chr -File -ErrorAction SilentlyContinue | ForEach-Object {
      $rel = $_.FullName.Substring($chartsRoot.Length+1)
      if ($rel -like 'Default\*') {
        Write-Output ($term + '|' + $_.FullName)
      }
    }
  }
}
""".replace("<ROOT>", ROOT)

status, out = run_ps(TAG, script)
paths = []
for line in out.splitlines():
    line = line.strip()
    if "|" in line:
        term, full = line.split("|", 1)
        paths.append((term, full.strip()))
print(f"Default profile files on {TAG}: {len(paths)}")

# download each, store as <term>__<basename>
result = {}
by_term = {}
for term, full in paths:
    by_term.setdefault(term, []).append(full)

for term, files in by_term.items():
    local_dir = os.path.join(OUT, term)
    os.makedirs(local_dir, exist_ok=True)
    dl = sftp_download_many(TAG, files, local_dir)
    ok = sum(1 for v in dl.values() if v)
    print(f"  {term}: {ok}/{len(files)} downloaded")
