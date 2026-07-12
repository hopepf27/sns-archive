#!/usr/bin/env bash
# 各スクリプトから読み込まれる共通部（直接実行しない）
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$ROOT/venv-linux"          # Windowsのvenvと衝突しないよう別名
PY="$VENV/bin/python"

need_setup() {
  if [ ! -x "$PY" ]; then
    echo "[エラー] 先に linux/setup.sh を実行してください。" >&2
    exit 1
  fi
}
need_config() {
  if [ ! -f "$ROOT/config.json" ]; then
    echo "[エラー] config.json がありません。" >&2
    echo "setup.sh 実行後、config.json に設定を記入してください。" >&2
    exit 1
  fi
}
