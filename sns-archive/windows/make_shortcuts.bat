@echo off
cd /d "%~dp0.."
echo タスクバーに置けるショートカットを作成します...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$root = (Get-Location).Path;" ^
  "$s = $ws.CreateShortcut($root + '\windows\アーカイブ ビューア.lnk');" ^
  "$s.TargetPath = $env:ComSpec;" ^
  "$s.Arguments = '/c ""' + $root + '\windows\start.bat""';" ^
  "$s.WorkingDirectory = $root;" ^
  "$s.IconLocation = $root + '\icons\viewer.ico';" ^
  "$s.Description = '統合SNSアーカイブ ビューアを起動';" ^
  "$s.Save();" ^
  "$s2 = $ws.CreateShortcut($root + '\windows\アーカイブ 同期.lnk');" ^
  "$s2.TargetPath = $env:ComSpec;" ^
  "$s2.Arguments = '/c ""' + $root + '\windows\sync_all.bat""';" ^
  "$s2.WorkingDirectory = $root;" ^
  "$s2.IconLocation = $root + '\icons\sync.ico';" ^
  "$s2.Description = '統合SNSアーカイブ すべて同期';" ^
  "$s2.Save()"
if errorlevel 1 (
    echo [エラー] ショートカットの作成に失敗しました。
    pause
    exit /b 1
)
echo.
echo この windows フォルダに2つのショートカットを作成しました:
echo   ・アーカイブ ビューア  … start.bat を起動
echo   ・アーカイブ 同期      … sync_all.bat を実行
echo.
echo 右クリック→「タスクバーにピン留めする」で固定できます。
echo （デスクトップ等へのコピー・移動も自由です）
pause
