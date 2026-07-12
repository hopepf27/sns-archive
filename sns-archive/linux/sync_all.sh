#!/usr/bin/env bash
# 同期を実行する。
#   linux/sync_all.sh                    … 4サービスすべて
#   linux/sync_all.sh misskey mastodon   … 指定したサービスだけ
#   linux/sync_all.sh --full             … オプションは各サービスへそのまま渡る
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_setup; need_config
cd "$ROOT"

targets=()
passthru=()
for a in "$@"; do
  case "$a" in
    twitter|misskey|bluesky|mastodon) targets+=("$a") ;;
    *) passthru+=("$a") ;;
  esac
done
[ "${#targets[@]}" -eq 0 ] && targets=(twitter misskey bluesky mastodon)

rc=0
for t in "${targets[@]}"; do
  echo "========== ${t} 同期 =========="
  "$PY" "ingest_${t}.py" ${passthru[@]+"${passthru[@]}"} || rc=1
  echo
done
[ "$rc" -eq 0 ] && echo "すべての同期が終わりました。" || echo "！一部の同期でエラーがありました（上のログを確認してください）。"
exit "$rc"
