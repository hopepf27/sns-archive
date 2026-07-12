#!/usr/bin/env bash
# ビューアを手動起動（Ctrl+Cで終了）。--host等の引数はそのまま渡せる
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_setup; need_config
cd "$ROOT"
exec "$PY" app.py --no-browser "$@"
