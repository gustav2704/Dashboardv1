param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$pnpm = 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\pnpm.cmd'
$basePython = 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & $basePython -m venv (Join-Path $root '.venv')
    & $python -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements.txt')
}
$dist = Join-Path $root 'frontend\dist\index.html'
if (-not (Test-Path -LiteralPath $dist)) {
    Push-Location (Join-Path $root 'frontend')
    try { & $pnpm install --frozen-lockfile; & $pnpm build } finally { Pop-Location }
}
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8765' }
Push-Location (Join-Path $root 'backend')
try { & $python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 } finally { Pop-Location }

