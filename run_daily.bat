@echo off
REM Daily URL QC runner — used by Windows Task Scheduler.
REM %~dp0 = folder this .bat lives in, so it works regardless of where it's called from.
cd /d "%~dp0"
if not exist logs mkdir logs

REM Throttle: the task fires at every logon (system on / restart) AND daily at 07:00, so
REM skip this run if the last actual run started less than 12 hours ago. throttle_check.ps1
REM prints RUN or SKIP and, on RUN, stamps logs\last_run.txt with the current time.
set "QC_DECIDE=RUN"
for /f "usebackq delims=" %%R in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scheduler\throttle_check.ps1" -MinHours 12`) do set "QC_DECIDE=%%R"
if /I "%QC_DECIDE%"=="SKIP" (
  echo ===== Skipped %date% %time% ^(last run under 12h ago^) ===== >> "logs\run.log"
  exit /b 0
)

echo ===== Run started %date% %time% ===== >> "logs\run.log"
REM Direct-URL QC is the default: QC only the links present in the sheet's redemption_url
REM column, opened directly (no Bifrost API, no redirect pre-resolution). URL prep is a
REM separate project's job; any blank row is skipped.
REM --email: once the run completes, email the TEMPLATE/WARN/FAIL rows (brand, merchant,
REM redemption_url, result) to the address in email_config.json / QC_EMAIL_TO.
python -u url_qc.py --email >> "logs\run.log" 2>&1
echo ===== Run finished %date% %time% ===== >> "logs\run.log"
