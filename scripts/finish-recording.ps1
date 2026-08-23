param(
    [Parameter(Mandatory = $true)][int]$BackendPid,
    [Parameter(Mandatory = $true)][int]$TrafficPid,
    [Parameter(Mandatory = $true)][int]$CollectorPid,
    [int]$SchedulerPid = 0,
    [Parameter(Mandatory = $true)][string]$StatusPath
)

$ErrorActionPreference = 'SilentlyContinue'

# Cache handles before waiting. Wait-Process -PassThru is unavailable in Windows
# PowerShell 5.1, and a missing exit code must never be treated as success.
$collectorProcess = Get-Process -Id $CollectorPid
$trafficProcess = Get-Process -Id $TrafficPid
$schedulerProcess = if ($SchedulerPid -gt 0) { Get-Process -Id $SchedulerPid } else { $null }
$backendProcess = Get-Process -Id $BackendPid
foreach ($recordingProcess in @($collectorProcess, $trafficProcess, $schedulerProcess, $backendProcess)) {
    if ($null -ne $recordingProcess) { $null = $recordingProcess.Handle }
}
$collectorExit = $null
if ($null -ne $collectorProcess) {
    $collectorProcess.WaitForExit()
    $collectorExit = $collectorProcess.ExitCode
}
$trafficExit = $null
if ($null -ne $trafficProcess) {
    $trafficProcess.WaitForExit()
    $trafficExit = $trafficProcess.ExitCode
}
$schedulerExit = $null
if ($null -ne $schedulerProcess) {
    $schedulerProcess.WaitForExit()
    $schedulerExit = $schedulerProcess.ExitCode
}

# Stop only the process tree created for this recording session.
if ($null -ne $backendProcess -and -not $backendProcess.HasExited) {
    & taskkill.exe /PID $BackendPid /T /F | Out-Null
}

if (Test-Path -LiteralPath $StatusPath) {
    $status = Get-Content -Raw -LiteralPath $StatusPath | ConvertFrom-Json
    $codes = @($collectorExit, $trafficExit)
    if ($SchedulerPid -gt 0) { $codes += $schedulerExit }
    $failed = @($codes | Where-Object { $null -ne $_ -and $_ -ne 0 }).Count -gt 0
    $unknown = ($null -eq $collectorExit -or $null -eq $trafficExit -or ($SchedulerPid -gt 0 -and $null -eq $schedulerExit))
    $status.stage = if ($failed) { 'failed' } elseif ($unknown) { 'needs_verification' } else { 'complete' }
    $status | Add-Member -NotePropertyName finished_at -NotePropertyValue ((Get-Date).ToUniversalTime().ToString('o')) -Force
    $status | Add-Member -NotePropertyName collector_exit_code -NotePropertyValue $collectorExit -Force
    $status | Add-Member -NotePropertyName traffic_exit_code -NotePropertyValue $trafficExit -Force
    if ($SchedulerPid -gt 0) {
        $status | Add-Member -NotePropertyName scheduler_exit_code -NotePropertyValue $schedulerExit -Force
    }
    $temporaryStatus = "$StatusPath.tmp"
    $status | ConvertTo-Json | Set-Content -LiteralPath $temporaryStatus -Encoding utf8
    Move-Item -LiteralPath $temporaryStatus -Destination $StatusPath -Force
}
