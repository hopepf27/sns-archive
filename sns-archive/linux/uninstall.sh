#!/usr/bin/env bash
# サービス・タイマーの登録を解除する（データ・設定・プログラムは消えません）
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
UNIT_DIR="$HOME/.config/systemd/user"
if command -v systemctl >/dev/null; then
  systemctl --user disable --now sns-archive.service 2>/dev/null || true
  systemctl --user disable --now sns-archive-sync.timer 2>/dev/null || true
fi
rm -f "$UNIT_DIR/sns-archive.service" \
      "$UNIT_DIR/sns-archive-sync.service" \
      "$UNIT_DIR/sns-archive-sync.timer"
command -v systemctl >/dev/null && systemctl --user daemon-reload || true
echo "サービス・タイマーの登録を解除しました。"
