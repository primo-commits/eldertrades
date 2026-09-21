@echo off
title Elder Trades - Health Check
cd /d "%~dp0"
echo.
echo  ============================================================
echo    STEP 2 of 4  --  Checking everything before the market opens
echo  ============================================================
echo.
python -m elder.preflight --scan
echo.
echo  ------------------------------------------------------------
echo   Look for PREFLIGHT PASSED above.
echo   If it says FAILED, scroll up and read the [FAIL] lines.
echo  ------------------------------------------------------------
echo.
pause
