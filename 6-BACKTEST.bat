@echo off
title Elder Trades - Backtest
cd /d "%~dp0"

echo.
echo  ============================================================
echo    BACKTEST -- measure the strategy against real history
echo  ============================================================
echo.
echo   1. Quick in-sample run   ~2 min   (NOT trustworthy alone)
echo   2. WALK-FORWARD          ~10 min  (out-of-sample -- the real answer)
echo   3. Sweep CONTEXT         ~20 min  (is the 4H bias read too strict?)
echo   4. Sweep CONFIRMATION    ~30 min  (is the entry trigger too strict?)
echo   5. Sweep LOCATION        ~20 min  (zone detection and proximity)
echo   6. Synthetic self-test   ~2 min   (no API keys needed)
echo.
echo   These take MINUTES to HOURS. Progress prints as it goes --
echo   if nothing appears for a few minutes it is still fetching data.
echo.

set choice=
set /p choice="Pick 1-6: "

REM Direct calls. The previous version routed through PowerShell Tee-Object,
REM which BUFFERS output -- a long run printed nothing until it finished and
REM looked frozen. Logging now happens inside Python, unbuffered.
if "%choice%"=="1" goto one
if "%choice%"=="2" goto two
if "%choice%"=="3" goto three
if "%choice%"=="4" goto four
if "%choice%"=="5" goto five
if "%choice%"=="6" goto six
echo.
echo  "%choice%" is not 1-6.
goto done

:one
python -m backtest.run --days 180
goto done
:two
python -m backtest.run --walk-forward --days 365 --train 60 --test 20
goto done
:three
python -m backtest.run --sweep context --days 180
goto done
:four
python -m backtest.run --sweep confirmation --days 180
goto done
:five
python -m backtest.run --sweep location --days 180
goto done
:six
python -m backtest.run --synthetic
goto done

:done
echo.
echo  ------------------------------------------------------------
echo   Everything written to backtest_results\
echo     console_*.log           full output of this run
echo     why_nothing_fired.csv   which gate blocked, and how often
echo     walk_forward.csv        per-window P^&L
echo     sweep_*.csv             one row per parameter combination
echo  ------------------------------------------------------------
echo.
pause
