@echo off
REM One-click setup: Python (install if missing) -> deps -> GSI config -> doctor.
REM NOTE: pure ASCII + CRLF; cmd.exe mis-parses UTF-8 Chinese and LF-only endings.
REM What it does, in order (it prints each step, nothing is silent):
REM   1) look for Python (python.exe on PATH, else the per-user install path)
REM   2) if missing: try winget --scope user; else download the official 3.12 installer
REM      and run it silently for the current user (no admin prompt, PATH gets updated)
REM   3) pip install -r requirements.txt
REM   4) install the GSI config into CS2's cfg folder (restart CS2 afterwards)
REM   5) run the environment check
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"

set "PYVER=3.12.7"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
set "PYPATH=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
set "PY="

echo ==============================================================================
echo  c4timer one-click setup
echo ==============================================================================

echo.
echo [1/5] looking for Python...
where python >nul 2>nul && set "PY=python"
if not defined PY if exist "%PYPATH%" set "PY=%PYPATH%"
if defined PY goto :have_python

echo       not found. Trying winget (per-user install, no admin prompt)...
where winget >nul 2>nul
if errorlevel 1 goto :download

winget install --id Python.Python.3.12 --scope user --silent ^
  --accept-package-agreements --accept-source-agreements
if exist "%PYPATH%" set "PY=%PYPATH%"
if not defined PY where python >nul 2>nul && set "PY=python"
if defined PY goto :have_python

:download
echo       winget unavailable or failed. Downloading the official installer...
curl -L --fail -o "%TEMP%\python-%PYVER%-amd64.exe" "%PYURL%"
if errorlevel 1 goto :manual
echo       running the installer silently (current user, PATH + pip included)...
"%TEMP%\python-%PYVER%-amd64.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
if exist "%PYPATH%" set "PY=%PYPATH%"
if not defined PY goto :manual

:have_python
echo       using: %PY%
"%PY%" -V
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if errorlevel 1 echo       [!] Python older than 3.9 - this project is only tested on 3.12.

echo.
echo [2/5] installing dependencies (numpy)...
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pip_failed

echo.
echo [3/5] installing the GSI config into CS2...
"%PY%" -m c4timer install-gsi

echo.
echo [4/5] environment check...
"%PY%" -m c4timer doctor

echo.
echo [5/5] done.
echo   * If the GSI config was just installed, RESTART CS2 (cfg is read at startup/map load).
echo   * Then double-click 启动.bat to play.
echo   * Optional, for better accuracy: play one round with 启动.bat running,
echo     then double-click 标定音频.bat once.
echo.
echo press any key to close.
pause >nul
exit /b 0

:manual
echo.
echo [X] Could not install Python automatically (no winget, and the download failed).
echo     Please install Python 3.12 by hand, ticking "Add python.exe to PATH":
echo     https://www.python.org/downloads/windows/
echo     Then run this file again.
echo.
pause >nul
exit /b 1

:pip_failed
echo.
echo [X] "pip install" failed. Most common causes:
echo     * no internet connection
echo     * Python installed without pip -> re-run the installer and tick "pip"
echo     You can also run it by hand:  "%PY%" -m pip install -r requirements.txt
echo.
pause >nul
exit /b 1
