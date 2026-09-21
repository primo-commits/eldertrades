@echo off
cd /d "%~dp0"
set LOGFILE=elder_trades_%date:~-4%%date:~4,2%%date:~7,2%.log
echo [$(date /t && time /t)] Bot starting — BTC/USD LIVE $100K >> "%LOGFILE%"
python elder_trades_paper_trader.py --crypto --live --balance 100000 --interval 5 >> "%LOGFILE%" 2>&1
echo [$(date /t && time /t)] Bot exited with code %ERRORLEVEL% >> "%LOGFILE%"
pause
