@echo off
chcp 65001 >nul
title NOT soundLARP 100%% - отладка
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=py"
echo Закрываю старую копию плеера, если она есть...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains(%~dp0player.py) -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
echo.
"%PY%" player.py --check
echo.
echo Запускаю плеер. Это окно показывает ошибки; закрой его, чтобы остановить плеер.
echo.
"%PY%" player.py
echo.
pause
