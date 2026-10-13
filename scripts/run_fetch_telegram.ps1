Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

Set-Location "E:\dohss_pipeline"
$env:DOHSS_CONFIG = "E:\dohss_pipeline\config.local.yaml"

$logDir = "E:\dohss_pipeline\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "telegram_fetch.log"

$start = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logFile -Value "`n[$start] Starting fetch-telegram"

& "E:\dohss_pipeline\.venv\Scripts\python.exe" "E:\dohss_pipeline\main.py" fetch-telegram *>> $logFile

$code = $LASTEXITCODE
$end = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logFile -Value "[$end] Finished exit_code=$code"

exit $code
