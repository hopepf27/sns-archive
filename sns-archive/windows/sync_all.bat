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

echo ========== 1/4 Twitter アーカイブ取り込み ==========
venv\Scripts\python ingest_twitter.py %*
echo.
echo ========== 2/4 Misskey 同期 ==========
venv\Scripts\python ingest_misskey.py %*
echo.
echo ========== 3/4 Bluesky 同期 ==========
venv\Scripts\python ingest_bluesky.py %*
echo.
echo ========== 4/4 Mastodon 同期 ==========
venv\Scripts\python ingest_mastodon.py %*
echo.
echo すべての同期が終わりました。start.bat でビューアを起動できます。
pause
