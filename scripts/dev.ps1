# YMONEY development launcher.
# Starts: MoneyPrinterTurbo engine (optional), backend API, frontend dev server.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\dev.ps1  [-WithEngine]
param(
    [switch]$WithEngine
)

$root = Split-Path -Parent $PSScriptRoot

function Start-Detached($cmd) {
    Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd } | Out-Null
}

if ($WithEngine) {
    Write-Host "[ymoney] starting MoneyPrinterTurbo video engine on :8080"
    Start-Detached "cmd /c `"cd /d $root\MoneyPrinterTurbo && .venv\Scripts\python.exe main.py`""
    Start-Sleep 5
}

Write-Host "[ymoney] starting backend API on :8100 (mock mode)"
Start-Detached "cmd /c `"cd /d $root\backend && set VIDEO_ENGINE=mock&& set MOCK_LLM=true&& set MOCK_PUBLISHING=true&& set MOCK_ANALYTICS=true&& .venv\Scripts\python.exe -m uvicorn app.main:app --port 8100 > ..\server.log 2> ..\server.err.log`""

Start-Sleep 4
Write-Host "[ymoney] starting frontend dev server on :5173"
Start-Detached "cmd /c `"cd /d $root\frontend && npm run dev > ..\frontend.log 2>&1`""

Start-Sleep 6
try {
    $health = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8100/health" -TimeoutSec 5
    Write-Host "[ymoney] backend: $($health.Content)"
} catch {
    Write-Host "[ymoney] backend not responding — check backend\..\server.err.log"
}
Write-Host ""
Write-Host "YMONEY UI : http://localhost:5173"
Write-Host "API docs  : http://127.0.0.1:8100/docs"
Write-Host "Press START in the UI to begin autonomous operation."
