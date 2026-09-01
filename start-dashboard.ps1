param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$bundledPnpm = 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\pnpm.cmd'
$userPnpm = Join-Path $env:APPDATA 'npm\pnpm.cmd'
$pnpm = if (Test-Path -LiteralPath $bundledPnpm) {
    $bundledPnpm
} elseif (Test-Path -LiteralPath $userPnpm) {
    $userPnpm
} else {
    (Get-Command pnpm.cmd -ErrorAction Stop).Source
}
$basePython = 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$backend = Join-Path $root 'backend'
$listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    throw "Dashboard already running on http://127.0.0.1:8765 (PID $($listener.OwningProcess)). Stop that process before starting another instance."
}
if (-not (Test-Path -LiteralPath $python)) {
    & $basePython -m venv (Join-Path $root '.venv')
    & $python -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements.txt')
}
$dist = Join-Path $root 'frontend\dist\index.html'
$frontendRoot = Join-Path $root 'frontend'
$frontendInputs = @(
    Get-ChildItem -LiteralPath (Join-Path $frontendRoot 'src') -Recurse -File
    Get-Item -LiteralPath (Join-Path $frontendRoot 'package.json')
    Get-Item -LiteralPath (Join-Path $frontendRoot 'pnpm-lock.yaml')
    Get-Item -LiteralPath (Join-Path $frontendRoot 'vite.config.ts')
    Get-Item -LiteralPath (Join-Path $frontendRoot 'tsconfig.app.json')
)
$latestFrontendInput = $frontendInputs | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
$needsFrontendBuild = -not (Test-Path -LiteralPath $dist) -or $latestFrontendInput.LastWriteTimeUtc -gt (Get-Item -LiteralPath $dist).LastWriteTimeUtc
if ($needsFrontendBuild) {
    Push-Location (Join-Path $root 'frontend')
    try { & $pnpm install --frozen-lockfile; & $pnpm build } finally { Pop-Location }
}
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8765' }
Push-Location $backend
try {
    do {
        & $python -B -m uvicorn app.main:app --app-dir $backend --host 127.0.0.1 --port 8765
        $exitCode = $LASTEXITCODE
        Write-Warning "Dashboard se detuvo con código $exitCode. Reiniciando en 3 segundos..."
        Start-Sleep -Seconds 3
    } while ($true)
} finally {
    Pop-Location
}
