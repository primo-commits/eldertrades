@echo off
title Elder Trades - PRACTICE MODE (no orders)
cd /d "%~dp0"
echo.
echo  ============================================================
echo    STEP 3 of 4  --  PRACTICE MODE
echo    It watches and thinks. It places NO orders. Safe.
echo    Press Ctrl-C ONCE to stop.
echo  ============================================================
echo.
python -m elder.runner
echo.
pause
