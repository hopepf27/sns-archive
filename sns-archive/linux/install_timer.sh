#!/usr/bin/env bash
# 毎日 03:45 (日本時間) の自動同期を登録する。
#   linux/install_timer.sh                    … 4サービスすべて
#   linux/install_timer.sh misskey mastodon   … 指定サービスのみ
# サーバ本体のタイムゾーンが日本時間でなくても、正しく 03:45 JST に実行される。
set -eu
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_setup; need_config

TARGETS="$*"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/sns-archive-sync.service" <<UNIT
[Unit]
Description=SNS Archive daily sync

[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart=$SCRIPT_DIR/sync_all.sh $TARGETS
UNIT
cat > "$UNIT_DIR/sns-archive-sync.timer" <<UNIT
[Unit]
Description=SNS Archive daily sync at 03:45 JST

[Timer]
OnCalendar=*-*-* 03:45:00 Asia/Tokyo
Persistent=true

[Install]
WantedBy=timers.target
UNIT
echo "作成: sns-archive-sync.service / .timer（対象: ${TARGETS:-すべて}）"

if ! command -v systemctl >/dev/null; then
  echo "！systemctl が見つかりません。ユニットファイルのみ作成しました。" >&2
  exit 1
fi
systemctl --user daemon-reload
systemctl --user enable --now sns-archive-sync.timer
loginctl enable-linger "$USER" 2>/dev/null || true

echo
echo "毎日 03:45 (JST) の自動同期を有効化しました。"
echo "  次回の実行予定: systemctl --user list-timers sns-archive-sync.timer"
echo "  手動で今すぐ実行: systemctl --user start sns-archive-sync.service"
echo "  同期のログ:       journalctl --user -u sns-archive-sync -f"
