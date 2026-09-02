@echo off
REM Interactive URL QC runner — double-click to run and watch output on screen.
cd /d "%~dp0"
if not exist logs mkdir logs
echo ===== Run started %date% %time% =====
python -u url_qc.py
echo.
echo ===== Run finished %date% %time% =====
echo.
if exist reports start "" "%~dp0reports"
pause
