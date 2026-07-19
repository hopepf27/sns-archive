# -*- coding: utf-8 -*-
"""
common.py — 統合SNSアーカイブ 共通ユーティリティ
DBスキーマ、設定読み込み、タイムゾーン、メディアダウンロードなど。
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "archive.db"
MEDIA_DIR = ROOT / "media"
CONFIG_PATH = ROOT / "config.json"

# 差分同期: 取り込み済みの投稿がこの件数連続したら「以降は取得済み」とみなして
# APIの遡りを打ち切る。過去日時への挿入（インポート等）は拾えないため、
# 完全に取り直したいときは各 sync に --full を付ける。
KNOWN_STREAK = 200

USER_AGENT = "sns-archive/1.0 (personal local archiver)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY,
  service TEXT NOT NULL,            -- 'twitter' | 'misskey' | 'bluesky' | 'mastodon'
  account TEXT NOT NULL,            -- 例: @me / @me@misskey.example / @me.bsky.social
  post_id TEXT NOT NULL,            -- 元サービス上のID
  type TEXT NOT NULL,               -- 'post' | 'repost' | 'like'
  created_at TEXT,                  -- UTC ISO8601 (NULL = 日時不明)
  created_local TEXT,               -- ローカルタイムゾーンのISO8601（表示・検索用）
  text TEXT NOT NULL DEFAULT '',
  author_handle TEXT,               -- repost/like の元投稿者（postなら自分）
  author_name TEXT,
  url TEXT,                         -- 元投稿へのリンク
  is_reply INTEGER NOT NULL DEFAULT 0,
  extra TEXT,                       -- JSON: 付加情報
  ym TEXT GENERATED ALWAYS AS (substr(created_local,1,7)) VIRTUAL,  -- 月別集計用
  UNIQUE(service, account, type, post_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_local ON posts(created_local);
CREATE INDEX IF NOT EXISTS idx_posts_svc ON posts(service, account);
CREATE INDEX IF NOT EXISTS idx_posts_type ON posts(type);
CREATE INDEX IF NOT EXISTS idx_posts_ym ON posts(ym, service);

CREATE TABLE IF NOT EXISTS media(
  id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL,
  kind TEXT,                        -- image | gifv | video | remote_video | audio | file
  local_path TEXT,                  -- media/ からの相対パス（NULL = ローカル未保存）
  remote_url TEXT,
  alt TEXT,
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media_post ON media(post_id);

CREATE TABLE IF NOT EXISTS sync_state(
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS bookmarks(
  id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL,
  label TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(post_id, label)
);
CREATE INDEX IF NOT EXISTS idx_bm_label ON bookmarks(label);
CREATE INDEX IF NOT EXISTS idx_bm_post ON bookmarks(post_id);
"""

# ---------------------------------------------------------------
# 全文検索インデックス (FTS5 trigram)
#   - 日本語の部分一致検索を高速化する（3文字以上の検索語で有効）
#   - FTS5/trigramが使えない環境では自動的に従来のLIKE検索に戻る
# ---------------------------------------------------------------

_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS posts_fts_ai AFTER INSERT ON posts BEGIN
  INSERT INTO posts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS posts_fts_ad AFTER DELETE ON posts BEGIN
  INSERT INTO posts_fts(posts_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS posts_fts_au AFTER UPDATE OF text ON posts BEGIN
  INSERT INTO posts_fts(posts_fts, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO posts_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

_fts_ok = None    # このプロセスでFTSが使えるか（Noneは未判定）


def fts_enabled():
    return bool(_fts_ok)


def _ensure_fts(con):
    """FTS5 trigram インデックスを用意する。使えない環境では安全に諦める。"""
    global _fts_ok
    if _fts_ok is not None:
        return
    try:
        con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5("
            "text, content='posts', content_rowid='id', tokenize='trigram')")
        con.executescript(_FTS_TRIGGERS)
        if get_state(con, "fts_ready") != "1":
            n = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            if n:
                print(f"検索インデックスを構築しています（{n:,}件・初回のみ）…")
            con.execute("INSERT INTO posts_fts(posts_fts) VALUES('rebuild')")
            set_state(con, "fts_ready", "1")
            con.commit()
            if n:
                print("検索インデックスの構築が完了しました。")
        _fts_ok = True
    except sqlite3.OperationalError:
        # FTS5やtrigram非対応のSQLite。トリガーが残っていると書き込みが
        # 失敗するため取り除き、以後は従来のLIKE検索で動かす。
        _fts_ok = False
        try:
            for tg in ("posts_fts_ai", "posts_fts_ad", "posts_fts_au"):
                con.execute(f"DROP TRIGGER IF EXISTS {tg}")
            con.execute("DELETE FROM sync_state WHERE key='fts_ready'")
            con.commit()
        except sqlite3.OperationalError:
            pass


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(
            "config.json が見つかりません。\n"
            "config.example.json をコピーして config.json を作成し、内容を編集してください。"
        )
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"config.json の書式が壊れています: {e}")


def cleanup_extracted(root, keep=False):
    """zipから展開した一時フォルダを削除する。空になった親（*_archive_extracted）も掃除。
    root が None（元々フォルダ指定）や keep=True のときは何もしない。"""
    if not root or keep:
        return
    import shutil
    try:
        shutil.rmtree(root)
        parent = root.parent
        if parent.name.endswith("_archive_extracted") and parent.exists() \
                and not any(parent.iterdir()):
            parent.rmdir()
        print(f"展開した一時データを削除しました（{root.name}）。")
    except OSError as e:
        print(f"  （展開データの削除に失敗しました: {e}）")


def as_list(v):
    """設定値を必ずリストにする（単一オブジェクト書きでも複数書きでも動くように）。"""
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []


def get_tz(cfg):
    """設定のタイムゾーン（既定 Asia/Tokyo）を返す。"""
    name = (cfg or {}).get("timezone", "Asia/Tokyo")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        # tzdata が無い環境向けフォールバック（JST固定）
        return timezone(timedelta(hours=9))


def connect():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")   # WALと併用で安全かつ高速
    # 既存テーブルがあれば先に列を補ってから、スキーマ（IF NOT EXISTS）を適用する。
    _migrate(con)
    con.executescript(SCHEMA)
    _ensure_fts(con)
    return con


def _migrate(con):
    """既存DBを最新スキーマへ。月別集計用の ym 列を補う。
    posts テーブルがまだ無い新規DBでは何もしない（SCHEMAが作る）。"""
    has_posts = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posts'").fetchone()
    if not has_posts:
        return
    cols = {r[1] for r in con.execute("PRAGMA table_xinfo(posts)")}
    if "ym" not in cols:
        try:
            # 生成列（SQLite 3.31+）。既存行にも substr が自動適用される。
            con.execute(
                "ALTER TABLE posts ADD COLUMN ym TEXT "
                "GENERATED ALWAYS AS (substr(created_local,1,7)) VIRTUAL")
        except sqlite3.OperationalError:
            # 古いSQLite向けフォールバック: 通常列＋トリガで維持
            con.execute("ALTER TABLE posts ADD COLUMN ym TEXT")
            con.execute("UPDATE posts SET ym=substr(created_local,1,7)")
            con.executescript("""
                CREATE TRIGGER IF NOT EXISTS trg_ym_ins AFTER INSERT ON posts BEGIN
                  UPDATE posts SET ym=substr(NEW.created_local,1,7) WHERE id=NEW.id;
                END;
                CREATE TRIGGER IF NOT EXISTS trg_ym_upd AFTER UPDATE OF created_local ON posts BEGIN
                  UPDATE posts SET ym=substr(NEW.created_local,1,7) WHERE id=NEW.id;
                END;
            """)
        con.commit()


# ---------------- 日時 ----------------

def parse_iso(s):
    """ISO8601（Z対応・ミリ秒対応）→ aware datetime。失敗時 None。"""
    if not s:
        return None
    try:
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def iso_pair(dt, tz):
    """aware datetime → (UTC ISO文字列, ローカルISO文字列)"""
    if dt is None:
        return None, None
    u = dt.astimezone(timezone.utc)
    l = dt.astimezone(tz)
    return (
        u.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        l.strftime("%Y-%m-%dT%H:%M:%S"),
    )


# ---------------- DB操作 ----------------

def upsert_post(con, *, service, account, post_id, type, created_at, created_local,
                text, author_handle=None, author_name=None, url=None,
                is_reply=0, extra=None):
    """投稿を挿入（既存なら更新）。posts.id を返す。

    内容が既存レコードと完全に同一の場合はUPDATE自体を発行しない
    （全文検索索引の無駄な書き換えとWALへの書き込みを避けるため）。

    ただし今回のレコードが補完待ちスタブ（extra に export_stub）で、かつ既存レコードが
    スタブでない（＝すでに本文を持つ完全なデータ）の場合は、本文・作者を劣化上書き
    しない。これは同一アカウントを API とエクスポートの両方で取り込むといった構成で、
    完全な本文がスタブで潰れるのを防ぐため。
    """
    incoming_stub = bool(extra and extra.get("export_stub"))
    if incoming_stub:
        existing = con.execute(
            "SELECT id, extra FROM posts WHERE service=? AND account=? AND type=? "
            "AND post_id=?", (service, account, type, post_id)).fetchone()
        if existing:
            ex = json.loads(existing["extra"]) if existing["extra"] else {}
            if not ex.get("export_stub"):
                return existing["id"]   # 既存の完全レコードを保持

    extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
    con.execute(
        """INSERT INTO posts(service,account,post_id,type,created_at,created_local,
                             text,author_handle,author_name,url,is_reply,extra)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(service,account,type,post_id) DO UPDATE SET
             created_at=excluded.created_at,
             created_local=excluded.created_local,
             text=excluded.text,
             author_handle=excluded.author_handle,
             author_name=excluded.author_name,
             url=excluded.url,
             is_reply=excluded.is_reply,
             extra=excluded.extra
           WHERE posts.created_at    IS NOT excluded.created_at
              OR posts.created_local IS NOT excluded.created_local
              OR posts.text          IS NOT excluded.text
              OR posts.author_handle IS NOT excluded.author_handle
              OR posts.author_name   IS NOT excluded.author_name
              OR posts.url           IS NOT excluded.url
              OR posts.is_reply      IS NOT excluded.is_reply
              OR posts.extra         IS NOT excluded.extra""",
        (service, account, post_id, type, created_at, created_local,
         text or "", author_handle, author_name, url, int(bool(is_reply)), extra_json),
    )
    row = con.execute(
        "SELECT id FROM posts WHERE service=? AND account=? AND type=? AND post_id=?",
        (service, account, type, post_id),
    ).fetchone()
    return row[0]


def replace_media(con, pid, items):
    """items: [{kind, local_path, remote_url, alt}] で media を置き換える。

    items が空のときは既存のメディアを保持する。これは再同期や補完の際、
    メディア取得が一時的に失敗して空リストが渡された場合に、前回取得できていた
    メディアまで失わないようにするため。実際にメディアが付いている投稿は
    必ず1件以上の items を渡すので、空 = 消去確定ではなく「今回は無し／取れず」
    と解釈する。
    """
    if not items:
        return
    # 既存と完全に同一なら書き込まない（既知投稿の再取り込みで毎回
    # DELETE+INSERT が走るのを避ける）
    existing = [(r["kind"], r["local_path"], r["remote_url"], r["alt"])
                for r in con.execute(
                    "SELECT kind, local_path, remote_url, alt FROM media "
                    "WHERE post_id=? ORDER BY sort", (pid,))]
    incoming = [(m.get("kind"), m.get("local_path"), m.get("remote_url"), m.get("alt"))
                for m in items]
    if existing == incoming:
        return
    con.execute("DELETE FROM media WHERE post_id=?", (pid,))
    for i, m in enumerate(items):
        con.execute(
            "INSERT INTO media(post_id,kind,local_path,remote_url,alt,sort) VALUES(?,?,?,?,?,?)",
            (pid, m.get("kind"), m.get("local_path"), m.get("remote_url"),
             m.get("alt"), i),
        )


def existing_post_ids(con, service, account, types):
    q = ",".join("?" * len(types))
    rows = con.execute(
        f"SELECT post_id FROM posts WHERE service=? AND account=? AND type IN ({q})",
        [service, account, *types],
    ).fetchall()
    return {r[0] for r in rows}


def get_state(con, key, default=None):
    r = con.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_state(con, key, value):
    con.execute(
        "INSERT INTO sync_state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ---------------- ファイル・ダウンロード ----------------

EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/avif": ".avif", "image/heic": ".heic",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/aac": ".m4a",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/wav": ".wav",
    "audio/flac": ".flac",
}


def sanitize(name, limit=80):
    s = re.sub(r"[^0-9A-Za-z._-]+", "_", name or "")
    s = s.strip("._") or "file"
    return s[:limit]


def ext_for(mime=None, name=None):
    if name and "." in name:
        e = "." + name.rsplit(".", 1)[-1].lower()
        if 2 <= len(e) <= 6 and re.fullmatch(r"\.[0-9a-z]+", e):
            return e
    if mime:
        m = mime.split(";")[0].strip().lower()
        if m in EXT_BY_MIME:
            return EXT_BY_MIME[m]
    return ".bin"


def download(url, dest: Path, session, timeout=90, retries=3, quiet=False):
    """URLを dest に保存。既に存在すればスキップ。成功で True。"""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            return True
        for attempt in range(retries):
            try:
                r = session.get(url, stream=True, timeout=timeout,
                                headers={"User-Agent": USER_AGENT})
            except Exception as e:
                if attempt == retries - 1:
                    if not quiet:
                        print(f"    ! ダウンロード失敗 {url} : {e}")
                    return False
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(8 * (attempt + 1))
                continue
            if r.status_code >= 400:
                if not quiet:
                    print(f"    ! ダウンロード失敗 {url} : HTTP {r.status_code}")
                return False
            tmp = dest.with_name(dest.name + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, dest)
            return True
        return False
    except Exception as e:
        if not quiet:
            print(f"    ! ダウンロード失敗 {url} : {e}")
        return False
