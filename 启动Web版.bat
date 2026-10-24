@echo off
setlocal
cd /d "%~dp0"

title YiYan Studio V3 - Web

echo ===============================================
echo   YiYan Studio V3 - Web Edition
echo ===============================================
echo.

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" goto NO_VENV
if not exist "%~dp0web\package.json" goto NO_WEB
if not exist "%~dp0web\node_modules" goto NO_DEPS
if /i "%1"=="rebuild" goto BUILD
if not exist "%~dp0web\dist\index.html" goto BUILD
goto RUN

:BUILD
echo [build] frontend bundle missing, building...
call :FIND_NODE
if errorlevel 1 goto NO_NODE
pushd "%~dp0web"
call npm run build
if errorlevel 1 (
  popd
  echo.
  echo [error] frontend build failed. See output above.
  goto FAIL
)
popd
echo [build] done.
echo.
goto RUN

:RUN
echo [run] starting server. browser will open in a few seconds.
echo [run] close this window to stop.
echo.
"%PY%" -m server
goto END

:FIND_NODE
where node >nul 2>&1
if not errorlevel 1 exit /b 0
if exist "%ProgramFiles%\nodejs\node.exe" (
  set "PATH=%ProgramFiles%\nodejs;%PATH%"
  exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" (
  set "PATH=%LOCALAPPDATA%\Programs\nodejs;%PATH%"
  exit /b 0
)
exit /b 1

:NO_VENV
echo [error] .venv not found.
echo   setup:
echo     python -m venv .venv
echo     .venv\Scripts\python -m pip install -r requirements.txt
goto FAIL

:NO_WEB
echo [error] web\package.json not found.
goto FAIL

:NO_DEPS
echo [error] web\node_modules missing.
echo   setup:
echo     cd web
echo     npm install
goto FAIL

:NO_NODE
echo [error] Node.js not found. Install Node 18+ or add it to PATH.
goto FAIL

:FAIL
echo.
pause
exit /b 1

:END
pause
