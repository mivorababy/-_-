@echo off
chcp 65001 >nul
title NOT soundLARP 100%% - установка зависимостей
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=py"
echo Используется интерпретатор: %PY%
"%PY%" --version
if errorlevel 1 goto failed
echo.
echo Устанавливаю зависимости...
"%PY%" -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto failed
echo.
echo Проверяю Discord Rich Presence и онлайн-поиск...
"%PY%" -c "import pypresence, yt_dlp; print('Discord Rich Presence и поиск: OK')"
if errorlevel 1 goto failed
echo.
echo Готово. Discord будет показывать активность только NOT soundLARP 100%%.
echo Полностью закрой и снова открой плеер.
pause
exit /b 0
:failed
echo.
echo Ошибка установки. Проверь подключение к интернету и доступ к pip.
pause
exit /b 1
