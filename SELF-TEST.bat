@echo off
title Elder Trades - Self Test
cd /d "%~dp0"
echo Running offline tests (no internet, no account needed)...
echo.
python -m tests.test_pipeline
echo.
python -m tests.test_reconciler
echo.
pause
