@echo off
REM Elder Trades launcher (Windows).
REM
REM Replaces the original run_bot.bat, which had two defects:
REM   1. `echo [$(date /t && time /t)]` is bash syntax. cmd splits on &&, so it
REM      echoed a fragment and then tried to run `time /t)]` as a command. That
REM      is why the old log was full of bare "02:07 PM" lines.
REM   2. %date:~-4%%date:~4,2%%date:~7,2% assumes US "Fri 09/19/2025" format.
REM      On a machine returning ISO "2025-09-19" it produced the filename
REM      elder_trades_9-19-0-1.log -- exactly the file that was uploaded.
REM PowerShell gives an unambiguous date regardless of locale.

setlocal
cd /d "%~dp0.."

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
if not exist logs mkdir logs
set LOGFILE=logs\elder_trades_%TODAY%.log

if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat

echo [%date% %time%] starting: %* >> "%LOGFILE%"
python -m elder.runner %* >> "%LOGFILE%" 2>&1
echo [%date% %time%] exited with code %ERRORLEVEL% >> "%LOGFILE%"
endlocal
