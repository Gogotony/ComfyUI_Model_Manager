@echo off
chcp 65001 >nul
title Comfyui Model Manager

REM ---- 兩用偵測 ComfyUI 根目錄：可攜版與非可攜版只差一個目錄 ----
REM 模式一（非可攜版）：工具放在根目錄內層 → 父目錄即根目錄（有 main.py + models）
REM 模式二（可攜版）  ：工具與 ComfyUI 應用同層 → 根目錄 = 父目錄\ComfyUI
set SCRIPT_DIR=%~dp0
set CAND1=%SCRIPT_DIR%..
set COMFYUI_ROOT=
if exist "%CAND1%\main.py" if exist "%CAND1%\models" set "COMFYUI_ROOT=%CAND1%"
if not defined COMFYUI_ROOT if exist "%CAND1%\ComfyUI\main.py" if exist "%CAND1%\ComfyUI\models" set "COMFYUI_ROOT=%CAND1%\ComfyUI"
if not defined COMFYUI_ROOT (
    echo [ERROR] 找不到 ComfyUI 根目錄。
    echo         非可攜版：請放在根目錄內層（父目錄即根目錄）。
    echo         可攜版  ：請與 ComfyUI 應用資料夾同層。
    echo         亦可用環境變數 COMFYUI_ROOT 指定。
    pause
    exit /1
)

REM ---- 找 Python：優先 python_embeded（可攜版）→ venv → 系統 python ----
if exist "%COMFYUI_ROOT%\..\python_embeded\python.exe" (
    set PY=%COMFYUI_ROOT%\..\python_embeded\python.exe
) else if exist "%COMFYUI_ROOT%\venv\Scripts\python.exe" (
    set PY=%COMFYUI_ROOT%\venv\Scripts\python.exe
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] 找不到 Python。請安裝 Python 或使用 ComfyUI 可攜版 python_embeded。
        pause
        exit /1
    )
    set PY=python
)

REM ---- 連接埠：單一變數，伺服器與瀏覽器都用同一個 ----
set PORT=8190

echo ========================================
echo  Comfyui Model Manager
echo  ComfyUI root: %COMFYUI_ROOT%
echo  URL: http://127.0.0.1:%PORT%
echo ========================================
echo.

REM ---- 防呆：8190 已有本工具伺服器在跑時，不再重複啟動，直接開瀏覽器 ----
where curl >nul 2>&1
if not errorlevel 1 (
    curl -s -m 2 "http://127.0.0.1:%PORT%/" | findstr /C:"Comfyui Model Manager" >nul 2>&1
    if not errorlevel 1 (
        echo [INFO] 伺服器已在執行中（http://127.0.0.1:%PORT%/），直接開啟瀏覽器。
        start "" "http://127.0.0.1:%PORT%/"
        pause
        exit /0
    )
)

REM 背景啟動伺服器
start "" /b "%PY%" "%SCRIPT_DIR%standalone_server.py" --port %PORT%

REM 等待伺服器就緒（最多約 15 秒）再開瀏覽器
where curl >nul 2>&1
if errorlevel 1 goto fallback_wait
set /a WAIT=0
:waitloop
timeout /t 1 /nobreak >nul
curl -s -o nul "http://127.0.0.1:%PORT%/" && goto server_up
set /a WAIT+=1
if %WAIT% lss 15 goto waitloop
echo [WARN] 伺服器逾時未回應，仍嘗試開啟瀏覽器…
goto server_up
:fallback_wait
timeout /t 3 /nobreak >nul
:server_up
start "" "http://127.0.0.1:%PORT%/"

echo 伺服器已啟動，請在瀏覽器中操作；關閉本視窗會同時結束伺服器。
echo 若要停止伺服器：回到本視窗按 Ctrl+C 即可。
pause
