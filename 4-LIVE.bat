@echo off
title Elder Trades - LIVE (paper account)
cd /d "%~dp0"
echo.
echo  ============================================================
echo    STEP 4 of 4  --  LIVE on your PAPER account
echo    Real orders. Fake money. No real money is at risk.
echo    Press Ctrl-C ONCE to stop.
echo  ============================================================
echo.
set /p ok="Type YES and press Enter to start: "
if /i not "%ok%"=="YES" (
  echo Cancelled.
  pause
  exit /b
)
python -m elder.runner --live
echo.
pause
