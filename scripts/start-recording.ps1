param(
    [int]$Port = 8010,
    [double]$RecordMinutes = 60,
    [double]$TrafficMinutes = 65,
    [ValidateSet('baseline', 'calibration')][string]$Role = 'baseline'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runsRoot = Join-Path $projectRoot 'runs'
$dataRoot = Join-Path $projectRoot 'data'
$statusPath = Join-Path $runsRoot 'recording-status.json'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python environment is missing. Run uv sync --locked first.'
}

if (Test-Path -LiteralPath $statusPath) {
    $oldStatus = Get-Content -Raw -LiteralPath $statusPath | ConvertFrom-Json
    $oldPids = @($oldStatus.backend_pid, $oldStatus.traffic_pid, $oldStatus.collector_pid)
    $live = @($oldPids | Where-Object { $_ -and (Get-Process -Id $_ -ErrorAction SilentlyContinue) })
    if ($live.Count -gt 0) {
        throw 'A recording session is still running. Inspect runs/recording-status.json.'
    }
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw "Port $Port is already in use. Choose another port with -Port."
}

$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
$sessionDir = Join-Path $runsRoot "recording-$stamp"
$databasePath = Join-Path $dataRoot "$Role-$stamp.sqlite"
New-Item -ItemType Directory -Path $sessionDir -Force | Out-Null
New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null

$backendOut = Join-Path $sessionDir 'backend.stdout.log'
$backendErr = Join-Path $sessionDir 'backend.stderr.log'
$trafficOut = Join-Path $sessionDir 'traffic.stdout.log'
$trafficErr = Join-Path $sessionDir 'traffic.stderr.log'
$collectorOut = Join-Path $sessionDir 'collector.stdout.log'
$collectorErr = Join-Path $sessionDir 'collector.stderr.log'
$targetUrl = "http://127.0.0.1:$Port"

$backend = Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -ArgumentList @('-m', 'service_monitor.cli', 'serve', '--port', "$Port", '--enable-dev-scenarios') `
    -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr

try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        if ($backend.HasExited) {
            throw "Backend exited during startup. Read $backendErr"
        }
        try {
            $health = Invoke-RestMethod -Uri "$targetUrl/health" -TimeoutSec 1
            if ($health.status -eq 'ok') {
                $ready = $true
                break
            }
        } catch { }
        Start-Sleep -Milliseconds 200
    }
    if (-not $ready) {
        throw "Backend did not become healthy. Read $backendErr"
    }

    $traffic = Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -ArgumentList @('-m', 'service_monitor.cli', 'traffic', '--url', $targetUrl, '--profile', 'normal-cycle', '--duration-minutes', "$TrafficMinutes") `
        -RedirectStandardOutput $trafficOut -RedirectStandardError $trafficErr
    $collector = Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -ArgumentList @('-m', 'service_monitor.cli', 'collect', '--url', $targetUrl, '--db', $databasePath, '--duration-minutes', "$RecordMinutes") `
        -RedirectStandardOutput $collectorOut -RedirectStandardError $collectorErr

    foreach ($process in @($backend, $traffic, $collector)) {
        try { $process.PriorityClass = 'BelowNormal' } catch { }
    }

    $status = [ordered]@{
        stage = 'recording'
        role = $Role
        started_at = (Get-Date).ToUniversalTime().ToString('o')
        expected_collector_end = (Get-Date).AddMinutes($RecordMinutes).ToUniversalTime().ToString('o')
        expected_traffic_end = (Get-Date).AddMinutes($TrafficMinutes).ToUniversalTime().ToString('o')
        target_url = $targetUrl
        database = $databasePath
        session_directory = $sessionDir
        backend_pid = $backend.Id
        traffic_pid = $traffic.Id
        collector_pid = $collector.Id
        note = "$Role normal recording only; no fault scenarios and no model training."
    }
    $temporaryStatus = "$statusPath.tmp"
    $status | ConvertTo-Json | Set-Content -LiteralPath $temporaryStatus -Encoding utf8
    Move-Item -LiteralPath $temporaryStatus -Destination $statusPath -Force

    $watcherOut = Join-Path $sessionDir 'watcher.stdout.log'
    $watcherErr = Join-Path $sessionDir 'watcher.stderr.log'
    $watcher = Start-Process -FilePath 'powershell.exe' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'finish-recording.ps1'),
                        '-BackendPid', "$($backend.Id)", '-TrafficPid', "$($traffic.Id)",
                        '-CollectorPid', "$($collector.Id)", '-StatusPath', $statusPath) `
        -RedirectStandardOutput $watcherOut -RedirectStandardError $watcherErr
    $status.watcher_pid = $watcher.Id
    $status | ConvertTo-Json | Set-Content -LiteralPath $temporaryStatus -Encoding utf8
    Move-Item -LiteralPath $temporaryStatus -Destination $statusPath -Force

    Write-Output "$Role recording started in background."
    Write-Output "Status: $statusPath"
    Write-Output "Database: $databasePath"
} catch {
    if ($backend -and -not $backend.HasExited) {
        Stop-Process -Id $backend.Id -ErrorAction SilentlyContinue
    }
    throw
}
