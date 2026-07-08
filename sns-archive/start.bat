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

echo ビューアを起動しています... ブラウザが自動で開きます。
echo 終了するには、このウィンドウで Ctrl+C を押すか、ウィンドウを閉じてください。
venv\Scripts\python app.py
pause
