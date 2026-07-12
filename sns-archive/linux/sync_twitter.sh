#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_setup; need_config
cd "$ROOT"
exec "$PY" ingest_twitter.py "$@"
