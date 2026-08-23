param(
    [int]$Port = 8010,
    [switch]$WaitForRecording,
    [ValidateSet('validation', 'test')][string]$Role = 'validation'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runsRoot = Join-Path $projectRoot 'runs'
$dataRoot = Join-Path $projectRoot 'data'
$statusPath = Join-Path $runsRoot "$Role-status.json"
$recordingStatusPath = Join-Path $runsRoot 'recording-status.json'
$queueStatusPath = Join-Path $runsRoot "$Role-queue-status.json"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python environment is missing. Run uv sync --locked first.'
}

if (Test-Path -LiteralPath $recordingStatusPath) {
    $recordingStatus = Get-Content -Raw -LiteralPath $recordingStatusPath | ConvertFrom-Json
    $recordingPids = @($recordingStatus.backend_pid, $recordingStatus.traffic_pid,
                       $recordingStatus.collector_pid, $recordingStatus.watcher_pid)
    $liveRecording = @($recordingPids | Where-Object { $_ -and (Get-Process -Id $_ -ErrorAction SilentlyContinue) })
    if ($liveRecording.Count -gt 0) {
        if (-not $WaitForRecording -or -not $recordingStatus.watcher_pid) {
            throw "Another recording is still running. Inspect $recordingStatusPath"
        }
        [ordered]@{
            stage = 'queued'
            queued_at = (Get-Date).ToUniversalTime().ToString('o')
            waiting_for_watcher_pid = $recordingStatus.watcher_pid
            note = 'Validation will start automatically after the normal recording finishes.'
        } | ConvertTo-Json | Set-Content -LiteralPath $queueStatusPath -Encoding utf8
        Wait-Process -Id $recordingStatus.watcher_pid
    }
}

if (Test-Path -LiteralPath $statusPath) {
    $oldStatus = Get-Content -Raw -LiteralPath $statusPath | ConvertFrom-Json
    $oldPids = @($oldStatus.backend_pid, $oldStatus.traffic_pid, $oldStatus.collector_pid,
                 $oldStatus.scheduler_pid, $oldStatus.watcher_pid)
    $live = @($oldPids | Where-Object { $_ -and (Get-Process -Id $_ -ErrorAction SilentlyContinue) })
    if ($live.Count -gt 0) { throw "$Role is already running. Inspect $statusPath" }
}

if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Choose another port with -Port."
}

$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
$sessionDir = Join-Path $runsRoot "$Role-$stamp"
$databasePath = Join-Path $dataRoot "$Role-$stamp.sqlite"
$schedulePath = Join-Path $sessionDir 'schedule-status.json'
New-Item -ItemType Directory -Path $sessionDir -Force | Out-Null
New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
$targetUrl = "http://127.0.0.1:$Port"

$backend = Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -ArgumentList @('-m', 'service_monitor.cli', 'serve', '--port', "$Port", '--enable-dev-scenarios') `
    -RedirectStandardOutput (Join-Path $sessionDir 'backend.stdout.log') `
    -RedirectStandardError (Join-Path $sessionDir 'backend.stderr.log')

try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        if ($backend.HasExited) { throw 'Backend exited during startup.' }
        try {
            $health = Invoke-RestMethod -Uri "$targetUrl/health" -TimeoutSec 1
            if ($health.status -eq 'ok') { $ready = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 200
    }
    if (-not $ready) { throw 'Backend did not become healthy.' }

    $traffic = Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -ArgumentList @('-m', 'service_monitor.cli', 'traffic', '--url', $targetUrl,
                        '--profile', 'validation-cycle', '--duration-minutes', '85') `
        -RedirectStandardOutput (Join-Path $sessionDir 'traffic.stdout.log') `
        -RedirectStandardError (Join-Path $sessionDir 'traffic.stderr.log')
    $collector = Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -ArgumentList @('-m', 'service_monitor.cli', 'collect', '--url', $targetUrl,
                        '--db', $databasePath, '--duration-minutes', '80') `
        -RedirectStandardOutput (Join-Path $sessionDir 'collector.stdout.log') `
        -RedirectStandardError (Join-Path $sessionDir 'collector.stderr.log')
    $scheduler = Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -ArgumentList @('-m', 'service_monitor.validation', '--url', $targetUrl, '--out', $schedulePath,
                        '--kind', $Role) `
        -RedirectStandardOutput (Join-Path $sessionDir 'scheduler.stdout.log') `
        -RedirectStandardError (Join-Path $sessionDir 'scheduler.stderr.log')

    foreach ($process in @($backend, $traffic, $collector, $scheduler)) {
        try { $process.PriorityClass = 'BelowNormal' } catch { }
    }

    $status = [ordered]@{
        stage = $Role
        role = $Role
        started_at = (Get-Date).ToUniversalTime().ToString('o')
        expected_collector_end = (Get-Date).AddMinutes(80).ToUniversalTime().ToString('o')
        expected_traffic_end = (Get-Date).AddMinutes(85).ToUniversalTime().ToString('o')
        target_url = $targetUrl
        database = $databasePath
        session_directory = $sessionDir
        schedule_status = $schedulePath
        backend_pid = $backend.Id
        traffic_pid = $traffic.Id
        collector_pid = $collector.Id
        scheduler_pid = $scheduler.Id
        note = "Automatic ${Role}: healthy surge plus five bounded fault patterns and recovery gaps."
    }
    $temporaryStatus = "$statusPath.tmp"
    $status | ConvertTo-Json | Set-Content -LiteralPath $temporaryStatus -Encoding utf8
    Move-Item -LiteralPath $temporaryStatus -Destination $statusPath -Force

    $watcher = Start-Process -FilePath 'powershell.exe' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                        (Join-Path $PSScriptRoot 'finish-recording.ps1'),
                        '-BackendPid', "$($backend.Id)", '-TrafficPid', "$($traffic.Id)",
                        '-CollectorPid', "$($collector.Id)", '-SchedulerPid', "$($scheduler.Id)",
                        '-StatusPath', $statusPath) `
        -RedirectStandardOutput (Join-Path $sessionDir 'watcher.stdout.log') `
        -RedirectStandardError (Join-Path $sessionDir 'watcher.stderr.log')
    $status.watcher_pid = $watcher.Id
    $status | ConvertTo-Json | Set-Content -LiteralPath $temporaryStatus -Encoding utf8
    Move-Item -LiteralPath $temporaryStatus -Destination $statusPath -Force

    Write-Output "Automatic $Role started in background."
    Write-Output "Status: $statusPath"
    Write-Output "Database: $databasePath"
    if (Test-Path -LiteralPath $queueStatusPath) {
        [ordered]@{
            stage = 'launched'
            launched_at = (Get-Date).ToUniversalTime().ToString('o')
            validation_status = $statusPath
        } | ConvertTo-Json | Set-Content -LiteralPath $queueStatusPath -Encoding utf8
    }
} catch {
    if ($backend -and -not $backend.HasExited) {
        & taskkill.exe /PID $backend.Id /T /F | Out-Null
    }
    throw
}
