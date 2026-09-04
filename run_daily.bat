@echo off
REM Daily URL QC runner — used by Windows Task Scheduler.
REM %~dp0 = folder this .bat lives in, so it works regardless of where it's called from.
cd /d "%~dp0"
if not exist logs mkdir logs

REM Throttle: the task fires daily at 07:00 AND at every logon, so skip this run if the
REM last actual run started less than 6 hours ago. throttle_check.ps1 prints RUN or SKIP
REM and, on RUN, stamps logs\last_run.txt with the current time.
set "QC_DECIDE=RUN"
for /f "usebackq delims=" %%R in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scheduler\throttle_check.ps1" -MinHours 6`) do set "QC_DECIDE=%%R"
if /I "%QC_DECIDE%"=="SKIP" (
  echo ===== Skipped %date% %time% ^(last run under 6h ago^) ===== >> "logs\run.log"
  exit /b 0
)

echo ===== Run started %date% %time% ===== >> "logs\run.log"
REM --no-bifrost-api: no Bifrost token needed. QC BI/BIFROST offers using the
REM redemption_url filled into the sheet manually; skip any that are still blank.
REM --email: once the run completes, email the TEMPLATE/WARN/FAIL rows (brand, merchant,
REM redemption_url, result) to the address in email_config.json / QC_EMAIL_TO.
python -u url_qc.py --no-bifrost-api --email >> "logs\run.log" 2>&1
echo ===== Run finished %date% %time% ===== >> "logs\run.log"
