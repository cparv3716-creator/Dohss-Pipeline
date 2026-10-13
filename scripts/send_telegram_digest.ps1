Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

Set-Location "E:\dohss_pipeline"
$env:DOHSS_CONFIG = "E:\dohss_pipeline\config.local.yaml"

$logDir = "E:\dohss_pipeline\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "telegram_dohss_digest.log"

$start = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logFile -Value "`n[$start] Starting telegram-dohss-digest"

. "E:\dohss_pipeline\.venv\Scripts\Activate.ps1"
python "E:\dohss_pipeline\main.py" telegram-dohss-digest *>> $logFile

$code = $LASTEXITCODE
$end = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logFile -Value "[$end] Finished exit_code=$code"

exit $code
