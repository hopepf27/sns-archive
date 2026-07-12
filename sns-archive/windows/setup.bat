@echo off
cd /d "%~dp0.."
echo ==========================================
echo   統合SNSアーカイブ セットアップ
echo ==========================================
echo.

set "PYCMD="
where py >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD goto :nopython

%PYCMD% --version >nul 2>nul
if errorlevel 1 goto :nopython

echo Python を確認しました。
echo 仮想環境 (venv) を作成しています... しばらくお待ちください。
%PYCMD% -m venv venv
if not exist "venv\Scripts\python.exe" (
    echo [エラー] 仮想環境の作成に失敗しました。
    pause
    exit /b 1
)

echo 必要なライブラリをインストールしています...
venv\Scripts\python -m pip install --upgrade pip --quiet
venv\Scripts\python -m pip install -r requirements.txt --no-warn-script-location
if errorlevel 1 (
    echo [エラー] ライブラリのインストールに失敗しました。
    echo インターネット接続を確認して、もう一度実行してください。
    pause
    exit /b 1
)

if not exist "config.json" (
    copy config.example.json config.json >nul
    echo config.json を作成しました。
)

echo.
echo ==========================================
echo   セットアップ完了！ 次の手順:
echo    1. config.json をメモ帳などで開き、
echo       アカウント情報を記入する
echo    2. sync_all.bat でデータを取得する
echo    3. start.bat でビューアを起動する
echo ==========================================
pause
exit /b 0

:nopython
echo [エラー] Python が見つかりません。
echo https://www.python.org/downloads/ から Python 3.10 以降を
echo インストールしてから、もう一度実行してください。
echo （インストール画面で "Add python.exe to PATH" に
echo   チェックを入れるのを忘れずに）
pause
exit /b 1
