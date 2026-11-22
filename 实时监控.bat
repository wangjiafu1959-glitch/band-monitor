@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set TZ=Asia/Shanghai

set "PYBIN="
if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PYBIN=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if defined PYBIN goto found
py -3 --version >nul 2>nul
if not errorlevel 1 set "PYBIN=py -3"
if defined PYBIN goto found
python --version >nul 2>nul
if not errorlevel 1 set "PYBIN=python"
if defined PYBIN goto found
goto nopython

:found
%PYBIN% monitor.py
goto end

:nopython
echo [x] Python not found.

:end
pause
