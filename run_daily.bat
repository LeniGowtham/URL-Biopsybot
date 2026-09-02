@echo off
REM Daily URL QC runner — used by Windows Task Scheduler.
REM %~dp0 = folder this .bat lives in, so it works regardless of where it's called from.
cd /d "%~dp0"
if not exist logs mkdir logs
echo ===== Run started %date% %time% ===== >> "logs\run.log"
python -u url_qc.py >> "logs\run.log" 2>&1
echo ===== Run finished %date% %time% ===== >> "logs\run.log"
