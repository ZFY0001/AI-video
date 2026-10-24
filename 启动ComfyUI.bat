@echo off
REM ============================================================
REM  ComfyUI launcher for YiYanStudio
REM  Usage:
REM      start_comfyui.bat                 (auto-detect ComfyUI dir)
REM      start_comfyui.bat D:\ComfyUI      (explicit dir)
REM
REM  IMPORTANT: keep this file ASCII-only. cmd.exe reads .bat with the
REM  OEM codepage (GBK on Chinese Windows); UTF-8 Chinese text turns into
REM  garbage commands. That is why all comments here are English.
REM ============================================================
setlocal

set "COMFY_DIR=%~1"
if not defined COMFY_DIR set "COMFY_DIR=%COMFYUI_DIR%"

REM ---- 1. locate ComfyUI -------------------------------------------------
if defined COMFY_DIR goto :found_dir
for %%D in (
    "%USERPROFILE%\ComfyUI"
    "%USERPROFILE%\Documents\ComfyUI"
    "%~dp0ComfyUI"
    "C:\ComfyUI"
    "D:\ComfyUI"
) do (
    if exist "%%~D\main.py" (
        set "COMFY_DIR=%%~D"
        goto :found_dir
    )
)
echo.
echo   [ERROR] ComfyUI not found.
echo          Pass its directory explicitly:
echo              %~nx0 D:\ComfyUI
echo.
echo          Or clone it first:
echo              git clone https://github.com/comfyanonymous/ComfyUI
echo.
pause
exit /b 1
:found_dir

if not exist "%COMFY_DIR%\main.py" (
    echo.
    echo   [ERROR] No main.py under: %COMFY_DIR%
    echo.
    pause
    exit /b 1
)

cd /d "%COMFY_DIR%"
echo.
echo   ComfyUI directory : %COMFY_DIR%
title ComfyUI Server for YiYanStudio

REM ---- 2. locate Python --------------------------------------------------
set "PY="
if exist "%COMFY_DIR%\venv\Scripts\python.exe" set "PY=%COMFY_DIR%\venv\Scripts\python.exe"
if not defined PY if exist "%COMFY_DIR%\.venv\Scripts\python.exe" set "PY=%COMFY_DIR%\.venv\Scripts\python.exe"
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PY set "PY=%%P"
    )
)
if not defined PY (
    echo.
    echo   [ERROR] Python not found. Install Python 3.12 and create a venv:
    echo              cd /d %COMFY_DIR%
    echo              python -m venv venv
    echo              venv\Scripts\python.exe -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130
    echo              venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo   Python            : %PY%

REM ---- 3. sanity checks --------------------------------------------------
if not exist "%COMFY_DIR%\models\checkpoints\*.safetensors" (
    echo.
    echo   [WARN] No checkpoint in models\checkpoints\
    echo          ComfyUI will start but cannot generate images.
    echo          YiYanStudio will fall back to offline placeholder frames.
    echo          See: docs\ComfyUI-Setup.md
    echo.
)

REM ---- 4. run ------------------------------------------------------------
REM  --disable-dynamic-vram : do NOT reload the model on every prompt
REM      (without it: 37 reloads observed, +300s each)
REM  --lowvram              : Windows desktop already eats ~3GB of the 8GB
echo.
echo   Starting ComfyUI on http://127.0.0.1:8188
echo   Close this window to stop the server.
echo.

"%PY%" main.py --port 8188 --disable-dynamic-vram --lowvram --reserve-vram 0.5

echo.
echo   ComfyUI stopped.
endlocal
pause
