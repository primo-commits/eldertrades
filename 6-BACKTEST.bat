@echo off
title Elder Trades - Backtest
cd /d "%~dp0"
if not exist logs mkdir logs
if not exist backtest_results mkdir backtest_results
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set STAMP=%%i
set LOG=logs\backtest_%STAMP%.log

echo.
echo  ============================================================
echo    BACKTEST -- measure the strategy against real history
echo  ============================================================
echo.
echo   1. Quick in-sample run   (fast, NOT trustworthy alone)
echo   2. WALK-FORWARD          (out-of-sample -- the real answer)
echo   3. Sweep CONTEXT         (is the 4H bias read too strict?)
echo   4. Sweep CONFIRMATION    (is the entry trigger too strict?)
echo   5. Sweep LOCATION        (zone detection and proximity)
echo   6. Synthetic self-test   (no API keys needed)
echo.
set /p choice="Pick 1-6: "

if "%choice%"=="1" set CMD=python -m backtest.run --days 180
if "%choice%"=="2" set CMD=python -m backtest.run --walk-forward --days 365 --train 60 --test 20
if "%choice%"=="3" set CMD=python -m backtest.run --sweep context --days 180
if "%choice%"=="4" set CMD=python -m backtest.run --sweep confirmation --days 180
if "%choice%"=="5" set CMD=python -m backtest.run --sweep location --days 180
if "%choice%"=="6" set CMD=python -m backtest.run --synthetic

echo.
echo  Output is shown here AND saved to %LOG%
echo.
REM Tee-Object so the run is visible live and nothing is lost to scrollback.
powershell -NoProfile -Command "& { %CMD% 2>&1 | Tee-Object -FilePath '%LOG%' }"

echo.
echo  ------------------------------------------------------------
echo   Full console output : %LOG%
echo   Results + CSVs      : backtest_results\
echo     why_nothing_fired.csv   <- which gate blocked, and how often
echo     skip_by_window.csv      <- the same, per window
echo     walk_forward.csv        <- per-window P^&L
echo  ------------------------------------------------------------
echo.
pause
