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
echo   3. Parameter sweep       (in-sample, for finding candidates)
echo   4. Synthetic self-test   (no API keys needed)
echo.
set /p choice="Pick 1-4: "
if "%choice%"=="1" python -m backtest.run --days 180
if "%choice%"=="2" python -m backtest.run --walk-forward --days 365 --train 60 --test 20
if "%choice%"=="3" python -m backtest.run --sweep --days 180
if "%choice%"=="4" python -m backtest.run --synthetic
echo.
echo  Results written to backtest_results\
echo.
pause
