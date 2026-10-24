@echo off
setlocal
title YiYanStudio Client
cd /d "%~dp0"

REM ASCII only: cmd.exe reads .bat in the OEM codepage.

if not exist ".venv\Scripts\pythonw.exe" goto NO_ENV
if not exist "web\dist\index.html" goto NO_BUILD

start "" ".venv\Scripts\pythonw.exe" "%~dp0desktop.py"
exit /b 0

:NO_ENV
echo.
echo   [ERROR] .venv not found.
echo   Create it once in this folder:
echo       python -m venv .venv
echo       .venv\Scripts\pip install fastapi "uvicorn[standard]" Pillow PyYAML python-multipart pywebview
echo.
pause
exit /b 1

:NO_BUILD
echo.
echo   [ERROR] web\dist not found.
echo   Build the frontend first:
echo       cd web ^&^& npm install ^&^& npm run build
echo   Or run the Web launcher with the "rebuild" argument.
echo.
pause
exit /b 1
