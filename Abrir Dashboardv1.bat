@echo off
setlocal
cd /d "%~dp0"

start "Dashboardv1 Server" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dashboard.ps1" -NoBrowser

echo Iniciando Dashboardv1...
powershell.exe -NoProfile -Command "$url='http://127.0.0.1:8765/api/health'; $ready=$false; for($i=0; $i -lt 60; $i++){ try { $response=Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2; if($response.StatusCode -eq 200){ $ready=$true; break } } catch {}; Start-Sleep -Seconds 1 }; if(-not $ready){ exit 1 }"

if errorlevel 1 (
    echo No se pudo iniciar Dashboardv1. Revisa la ventana del servidor.
    pause
    exit /b 1
)

start "" "http://127.0.0.1:8765"
exit /b 0
