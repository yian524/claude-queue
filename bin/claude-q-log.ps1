# claude-q-log.ps1 - quick diagnostic: list recent sessions and dump log by session
#
# Usage:
#   claude-q-log              # list latest 10 sessions
#   claude-q-log -Latest      # dump the most recent session's log
#   claude-q-log -SessionId 20260424T185029-c11f31   # specific session
#   claude-q-log -Since '18:00'                      # dump all sessions modified after HH:MM today

[CmdletBinding()]
param(
    [switch]$Latest,
    [string]$SessionId,
    [string]$Since
)

$root = "$env:USERPROFILE\.claude\run\claude-q"
if (-not (Test-Path $root)) {
    Write-Host "claude-q run root not found: $root" -ForegroundColor Red
    exit 1
}

$dirs = Get-ChildItem $root -Directory | Sort-Object LastWriteTime -Descending

if ($SessionId) {
    $log = Join-Path $root "$SessionId\monitor.log"
    if (-not (Test-Path $log)) {
        Write-Host "no monitor.log in session $SessionId" -ForegroundColor Red
        exit 1
    }
    Write-Host "=== $SessionId ===" -ForegroundColor Cyan
    Get-Content $log
    exit 0
}

if ($Latest) {
    $s = $dirs | Select-Object -First 1
    $log = Join-Path $s.FullName "monitor.log"
    Write-Host "=== latest: $($s.Name) ($($s.LastWriteTime.ToString('HH:mm:ss'))) ===" -ForegroundColor Cyan
    if (Test-Path $log) { Get-Content $log } else { Write-Host "(no monitor.log)" }
    exit 0
}

if ($Since) {
    $today = (Get-Date).Date
    $cutoff = [datetime]::ParseExact("$($today.ToString('yyyy-MM-dd')) $Since", 'yyyy-MM-dd HH:mm', $null)
    $matching = $dirs | Where-Object { $_.LastWriteTime -ge $cutoff }
    foreach ($s in $matching) {
        $log = Join-Path $s.FullName "monitor.log"
        Write-Host "=== $($s.Name) (modified $($s.LastWriteTime.ToString('HH:mm:ss'))) ===" -ForegroundColor Cyan
        if (Test-Path $log) { Get-Content $log } else { Write-Host "(no monitor.log)" }
        Write-Host ""
    }
    exit 0
}

# default: list sessions
Write-Host "Recent claude-q sessions (most recent first):"
$dirs | Select-Object -First 10 | ForEach-Object {
    $log = Join-Path $_.FullName "monitor.log"
    $logSize = if (Test-Path $log) { "$([math]::Round((Get-Item $log).Length / 1KB, 1)) KB" } else { "(no log)" }
    "  {0}  {1}  {2}" -f $_.LastWriteTime.ToString('HH:mm:ss'), $_.Name, $logSize
}
Write-Host ""
Write-Host "Dump a specific session:   claude-q-log -SessionId <id>"
Write-Host "Dump the most recent:      claude-q-log -Latest"
Write-Host "Dump sessions after time:  claude-q-log -Since '18:00'"
