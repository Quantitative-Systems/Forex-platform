@echo off
:loop
powershell -Command "Get-Process -Id 16584 -ErrorAction SilentlyContinue | Where-Object { -not $_.HasExited }" > nul
if %errorlevel% equ 0 (
    timeout /t 60 > nul
    goto loop
)
echo %date% %time% CAMPAIGN FINISHED >> data\campaign_monitor.log
tail -n 10 data\campaign_stdout.log >> data\campaign_monitor.log 2>nul
echo --- >> data\campaign_monitor.log
