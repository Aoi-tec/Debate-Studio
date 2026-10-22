@echo off
rem Debate Studio - Windows launcher (double-click this file)
rem Options are passed through, e.g.  start.bat --demo
setlocal
cd /d "%~dp0"
chcp 65001 >nul

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 run.py %*
  goto :done
)
where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python run.py %*
  goto :done
)
echo.
echo [ERROR] Python 3.10 or later was not found.
echo   Install from https://www.python.org/downloads/  (check "Add python.exe to PATH")
echo   or run in PowerShell:  winget install Python.Python.3.12
echo.

:done
echo.
pause
