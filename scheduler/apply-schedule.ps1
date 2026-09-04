# apply-schedule.ps1
# Registers/refreshes the "URL QC Daily" scheduled task from the exported XML and
# makes sure Windows will WAKE the machine (from sleep/hibernate) to run it on both
# AC power and battery, and catch up a missed run when the machine next comes on.
#
# NOTE: No task can run while the PC is fully powered OFF (shut down / no power).
# WakeToRun only wakes the machine from Sleep or Hibernate. If you need runs while
# the laptop is truly off, run the job on an always-on machine or cloud environment.
#
# Usage:  right-click > Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File apply-schedule.ps1

$ErrorActionPreference = 'Stop'
$taskName = 'URL QC Daily'
$xmlPath  = Join-Path $PSScriptRoot 'URL-QC-Daily.task.xml'

Write-Host "Registering task '$taskName' from $xmlPath ..."
$xml = Get-Content -Path $xmlPath -Raw
Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Out-Null

# Belt-and-suspenders: enforce the wake/battery/catch-up settings even if the XML drifts.
$t = Get-ScheduledTask -TaskName $taskName
$t.Settings.DisallowStartIfOnBatteries = $false
$t.Settings.StopIfGoingOnBatteries     = $false
$t.Settings.WakeToRun                   = $true
$t.Settings.StartWhenAvailable          = $true
$t | Set-ScheduledTask | Out-Null

# Allow wake timers on both AC and battery for the active power scheme.
$scheme = (powercfg /getactivescheme)
$guid   = ($scheme -replace '.*GUID:\s*([0-9a-fA-F-]+).*', '$1')
powercfg /setacvalueindex $guid SUB_SLEEP BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D 1
powercfg /setdcvalueindex $guid SUB_SLEEP BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D 1
powercfg /setactive $guid

Write-Host "Done. Current settings:"
(Get-ScheduledTask -TaskName $taskName).Settings |
  Select-Object DisallowStartIfOnBatteries, StopIfGoingOnBatteries, WakeToRun, StartWhenAvailable, ExecutionTimeLimit |
  Format-List
