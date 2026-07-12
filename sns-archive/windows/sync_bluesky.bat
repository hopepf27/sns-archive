@echo off
cd /d "%~dp0.."
set PYTHONUTF8=1
if not exist "venv\Scripts\python.exe" (
    echo [エラー] 先に setup.bat を実行してください。
    pause
    exit /b 1
)
if not exist "config.json" (
    echo [エラー] config.json がありません。
    echo setup.bat を実行してから、config.json に設定を記入してください。
    pause
    exit /b 1
)

venv\Scripts\python ingest_bluesky.py %*
echo.
echo Blueskyの同期が終わりました。
pause
