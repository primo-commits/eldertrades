@echo off
title Elder Trades - Install
cd /d "%~dp0"
echo.
echo  ============================================================
echo    STEP 1 of 4  --  Installing what Python needs
echo  ============================================================
echo.
python -m pip install -r requirements.txt
echo.
if errorlevel 1 (
  echo  SOMETHING WENT WRONG. Is Python installed?
  echo  Try typing:  python --version
) else (
  echo  DONE. Now edit alpaca_keys.txt, then run 2-CHECK.bat
)
echo.
pause
