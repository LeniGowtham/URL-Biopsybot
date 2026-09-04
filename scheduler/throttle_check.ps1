# throttle_check.ps1
# Decides whether run_daily.bat should run now, based on when the last actual run started.
#
#   - If the marker file shows the last run started < MinHours ago  -> prints "SKIP"
#     (marker is left untouched, so repeated logons don't push the window forward).
#   - Otherwise -> stamps the marker with "now" and prints "RUN".
#
# The marker is only updated when we decide to RUN, so a skipped logon does not count
# as a run. Called by run_daily.bat:
#   for /f %%R in ('powershell -NoProfile -ExecutionPolicy Bypass -File scheduler\throttle_check.ps1 -MinHours 6') do set QC_DECIDE=%%R

param(
    [double]$MinHours = 12
)

$ErrorActionPreference = 'Stop'
$repoRoot   = Split-Path -Parent $PSScriptRoot           # scheduler\ -> repo root
$markerPath = Join-Path $repoRoot 'logs\last_run.txt'    # holds Unix seconds of last run start

$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

$last = $null
if (Test-Path $markerPath) {
    $raw = (Get-Content -Path $markerPath -Raw).Trim()
    [long]$parsed = 0
    if ([long]::TryParse($raw, [ref]$parsed)) { $last = $parsed }
}

if ($null -ne $last -and ($now - $last) -lt ($MinHours * 3600)) {
    # Too soon since the last run — skip, leave the marker as-is.
    Write-Output 'SKIP'
    exit 0
}

# We're going to run: stamp the marker with now (before the run) and greenlight.
[System.IO.File]::WriteAllText($markerPath, "$now", (New-Object System.Text.UTF8Encoding($false)))
Write-Output 'RUN'
exit 0
