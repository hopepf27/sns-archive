#!/usr/bin/env bash
# ビューアを systemd ユーザーサービスとして登録する（常時起動・自動再起動・OS起動時に開始）
set -eu
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_setup; need_config

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/sns-archive.service" <<UNIT
[Unit]
Description=SNS Archive Viewer
After=network-online.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$PY app.py --no-browser
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT
echo "作成: $UNIT_DIR/sns-archive.service"

if ! command -v systemctl >/dev/null; then
  echo "！systemctl が見つかりません。ユニットファイルのみ作成しました。" >&2
  exit 1
fi
systemctl --user daemon-reload
systemctl --user enable --now sns-archive.service
# ログアウト後・再起動後も動かし続けるための設定（失敗したら案内だけ出す）
loginctl enable-linger "$USER" 2>/dev/null \
  || echo "！lingerの有効化に失敗しました。再起動後も動かすには: sudo loginctl enable-linger $USER"

echo
echo "ビューアをサービスとして起動しました。"
echo "  状態確認:   systemctl --user status sns-archive"
echo "  ログ:       journalctl --user -u sns-archive -f"
echo "  停止/解除:  linux/uninstall.sh"
echo "  待ち受け先は config.json の \"host\" で変更できます（変更後: systemctl --user restart sns-archive）"
