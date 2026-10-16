@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYLAUNCHER_DEBUG="
set "PYLAUNCHER_DRYRUN="
set "PYMANAGER_DEBUG="
set "PYMANAGER_VERBOSE="
set "DISK_DOWNLOADER_BOOTSTRAP_ATTEMPTED="

if exist "%~dp0.venv\Scripts\" goto project_python
if exist "%~dp0.venv\bin\" goto find_managed
if exist "%~dp0.venv" goto broken_venv

:find_managed
if not defined LOCALAPPDATA goto find_python
if exist "%LOCALAPPDATA%\DiskDownloader\Python\python.exe" goto managed_python

:find_python
py.exe -3 -I -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if "%errorlevel%"=="0" goto use_py
python.exe -I -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if "%errorlevel%"=="0" goto use_python
python3.exe -I -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if "%errorlevel%"=="0" goto use_python3
goto install_python

:install_python
if not defined LOCALAPPDATA goto bootstrap_failed
if not exist "%~dp0bootstrap-python.ps1" goto bootstrap_failed
echo Подготовка Python для DiskDownloader...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap-python.ps1"
set "disk_launcher_exit=%errorlevel%"
if not "%disk_launcher_exit%"=="0" goto show_error
if not exist "%LOCALAPPDATA%\DiskDownloader\Python\python.exe" goto bootstrap_failed
set "DISK_DOWNLOADER_BOOTSTRAP_ATTEMPTED=1"

:managed_python
"%LOCALAPPDATA%\DiskDownloader\Python\python.exe" -I -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if "%errorlevel%"=="0" goto use_managed
if not "%DISK_DOWNLOADER_BOOTSTRAP_ATTEMPTED%"=="1" goto install_python
goto bootstrap_failed

:use_managed
"%LOCALAPPDATA%\DiskDownloader\Python\python.exe" -I -X utf8 "%~dp0launch_windows.py" gui %*
set "disk_launcher_exit=%errorlevel%"
goto finished

:bootstrap_failed
echo Не удалось подготовить Python автоматически. Распакуйте проект целиком и повторите запуск.
echo Python с поддержкой Tcl/Tk: https://www.python.org/downloads/windows/
set "disk_launcher_exit=1"
goto show_error

:project_python
if not exist "%~dp0.venv\Scripts\python.exe" goto broken_venv
"%~dp0.venv\Scripts\python.exe" -I -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not "%errorlevel%"=="0" goto broken_venv
"%~dp0.venv\Scripts\python.exe" -I -X utf8 "%~dp0launch_windows.py" gui %*
set "disk_launcher_exit=%errorlevel%"
goto finished

:use_py
py.exe -3 -I -X utf8 "%~dp0launch_windows.py" gui %*
set "disk_launcher_exit=%errorlevel%"
goto finished

:use_python
python.exe -I -X utf8 "%~dp0launch_windows.py" gui %*
set "disk_launcher_exit=%errorlevel%"
goto finished

:use_python3
python3.exe -I -X utf8 "%~dp0launch_windows.py" gui %*
set "disk_launcher_exit=%errorlevel%"
goto finished

:broken_venv
echo Не удалось запустить Python из Windows-окружения .venv.
echo Создайте Windows-окружение заново с Python 3.10 или новее и поддержкой Tcl/Tk.
set "disk_launcher_exit=1"
goto show_error

:finished
if "%disk_launcher_exit%"=="0" exit /b 0

:show_error
echo(
echo Нажмите любую клавишу, чтобы закрыть окно...
pause >nul
exit /b %disk_launcher_exit%
