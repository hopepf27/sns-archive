#!/usr/bin/env bash
# 初回セットアップ: Python仮想環境の作成と依存のインストール
set -eu
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if ! command -v python3 >/dev/null; then
  echo "[エラー] python3 が見つかりません。例: sudo apt install python3 python3-venv" >&2
  exit 1
fi
echo "仮想環境を作成しています → $VENV"
python3 -m venv "$VENV"
"$PY" -m pip install --upgrade pip -q
"$PY" -m pip install -r "$ROOT/requirements.txt" -q
if [ ! -f "$ROOT/config.json" ]; then
  cp "$ROOT/config.example.json" "$ROOT/config.json"
  echo "config.json を作成しました。エディタで開いて設定を記入してください。"
fi
echo
echo "セットアップ完了。"
echo "  ビューアの起動:            linux/start.sh"
echo "  常時起動（サービス化）:     linux/install_service.sh"
echo "  毎日03:45(JST)の自動同期:   linux/install_timer.sh"
