@echo off
title Elder Trades - Backtest
cd /d "%~dp0"
echo.
echo  ============================================================
echo    BACKTEST -- measure the strategy against real history
echo  ============================================================
echo.
echo   1. Quick in-sample run   (fast, but NOT trustworthy alone)
echo   2. WALK-FORWARD          (out-of-sample -- the real answer)
echo   3. Sweep CONTEXT         (is the 4H bias read too strict?)
echo   4. Sweep CONFIRMATION    (is the entry trigger too strict?)
echo   5. Sweep LOCATION        (zone detection and proximity)
echo   6. Synthetic self-test   (no API keys needed)
echo.
set /p choice="Pick 1-6: "
if "%choice%"=="1" python -m backtest.run --days 180
if "%choice%"=="2" python -m backtest.run --walk-forward --days 365 --train 60 --test 20
if "%choice%"=="3" python -m backtest.run --sweep context --days 180
if "%choice%"=="4" python -m backtest.run --sweep confirmation --days 180
if "%choice%"=="5" python -m backtest.run --sweep location --days 180
if "%choice%"=="6" python -m backtest.run --synthetic
echo.
echo  Results written to backtest_results\
echo.
pause
