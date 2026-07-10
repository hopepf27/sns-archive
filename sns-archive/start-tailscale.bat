@echo off
cd /d "%~dp0"
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

echo ビューア（Tailscale対応）を起動しています...
echo 終了するには、このウィンドウで Ctrl+C を押すか、ウィンドウを閉じてください。
venv\Scripts\python app.py --host あなたのTailscaleコンソールに表示されているデバイス名 --no-browser
pause
