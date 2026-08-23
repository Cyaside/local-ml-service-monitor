param(
    [string]$Model = 'models/checkout-universal-v1',
    [int]$Port = 8010,
    [double]$DurationMinutes = 60
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
$modelPath = Join-Path $projectRoot $Model

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Python environment is missing. Run uv sync --locked first.'
}
if (-not (Test-Path -LiteralPath (Join-Path $modelPath 'READY') -PathType Leaf)) {
    throw "Trained model not found: $modelPath"
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Choose another port with -Port."
}
if ($DurationMinutes -le 0 -or $DurationMinutes -gt 1440) {
    throw 'DurationMinutes must be greater than 0 and at most 1440.'
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmss')
$runDirectory = Join-Path $projectRoot "runs/live-monitor-$stamp"
New-Item -ItemType Directory -Path $runDirectory | Out-Null
$database = Join-Path $runDirectory 'monitor.sqlite'
$url = "http://127.0.0.1:$Port"
$backendOut = Join-Path $runDirectory 'backend.stdout.log'
$backendErr = Join-Path $runDirectory 'backend.stderr.log'
$trafficOut = Join-Path $runDirectory 'traffic.stdout.log'
$trafficErr = Join-Path $runDirectory 'traffic.stderr.log'
$backend = $null
$traffic = $null

Push-Location $projectRoot
try {
    $backend = Start-Process -FilePath $pythonPath -WindowStyle Hidden -PassThru `
        -ArgumentList @('-m', 'service_monitor.cli', 'serve', '--port', "$Port", '--enable-dev-scenarios') `
        -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr

    $healthy = $false
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        if ($backend.HasExited) { throw "Backend exited. Read $backendErr" }
        try {
            $response = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 1
            if ($response.status -eq 'ok') { $healthy = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 200
    }
    if (-not $healthy) { throw "Backend did not become healthy. Read $backendErr" }

    $traffic = Start-Process -FilePath $pythonPath -WindowStyle Hidden -PassThru `
        -ArgumentList @('-m', 'service_monitor.cli', 'traffic', '--url', $url,
                        '--duration-minutes', "$DurationMinutes", '--rate', '3', '--profile', 'constant') `
        -RedirectStandardOutput $trafficOut -RedirectStandardError $trafficErr

    Write-Host ''
    Write-Host 'LOCAL ML SERVICE MONITOR' -ForegroundColor Cyan
    Write-Host "Target   : $url" -ForegroundColor DarkGray
    Write-Host "Model    : $modelPath" -ForegroundColor DarkGray
    Write-Host "Database : $database" -ForegroundColor DarkGray
    Write-Host 'Status   : LIVE' -ForegroundColor Green
    Write-Host ''
    Write-Host 'The model needs about five minutes of telemetry before scoring.' -ForegroundColor Yellow
    Write-Host "To inject a test error from another terminal:" -ForegroundColor DarkGray
    Write-Host "uv run service-monitor scenario --url $url --name error-burst --error-probability 0.2 --duration-seconds 120" -ForegroundColor White
    Write-Host ''

    & $pythonPath -m service_monitor.cli monitor --url $url --model $modelPath `
        --db $database --duration-minutes "$DurationMinutes" --allow-experimental --pretty
    if ($LASTEXITCODE -ne 0) { throw "Monitor failed with exit code $LASTEXITCODE" }
} finally {
    foreach ($process in @($traffic, $backend)) {
        if ($null -ne $process -and -not $process.HasExited) {
            & taskkill.exe /PID $($process.Id) /T /F 2>$null | Out-Null
        }
    }
    Pop-Location
}
