# -*- coding: utf-8 -*-
"""
ingest_mastodon.py — Mastodon の投稿・ブースト・お気に入りを取り込む

2つの取り込み方法（config.json の mastodon の各エントリごとに選択）:
  A) API取得（推奨）: "host" と "token" を書く
     - 投稿・ブースト（本文つき）・お気に入りを取得。2回目以降は差分同期。
  B) アーカイブ取り込み: "archive" にエクスポートzip（または展開済みフォルダ）を書く
     - outbox.json（投稿・ブースト）と likes.json（お気に入り）を読む。
     - アーカイブの仕様上、ブーストとお気に入りは元投稿の本文を含みません。
     - お気に入りの日時は元投稿ID（Mastodon Snowflake）から復元した推定値です。

オプション:
  --full             差分ではなく全件を取得し直す（API取得時）
  --skip-media       メディアをダウンロード/コピーしない
  --skip-like-media  お気に入りのメディアはダウンロードしない
"""
import argparse
import json
import re
import sys
import time
import zipfile
import shutil
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

import common

PAGE_LIMIT = 40
SLEEP = 0.4          # APIコール間の待機（レート制限にやさしく）
DL_SLEEP = 0.15


class MdError(Exception):
    pass


# ---------------------------------------------------------------
# HTML → プレーンテキスト
# Mastodonの本文はHTML。<p>や<br>を改行に、リンクはURL/タグ/メンション表記に戻す。
# ---------------------------------------------------------------

class ContentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []          # 出力バッファ
        self.a_stack = []        # (href, classes, 内側テキストのバッファ)
        self.skip_depth = 0      # class="invisible" の span 内は無視

    def _emit(self, s):
        if self.skip_depth:
            return
        if self.a_stack:
            self.a_stack[-1][2].append(s)
        else:
            self.parts.append(s)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = (d.get("class") or "").split()
        if tag == "br":
            self._emit("\n")
        elif tag == "p" and self.parts:
            self.parts.append("\n\n")
        elif tag == "span" and "invisible" in cls:
            self.skip_depth += 1
        elif tag == "a":
            self.a_stack.append((d.get("href") or "", cls, []))

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag == "span" and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "a" and self.a_stack:
            href, cls, buf = self.a_stack.pop()
            inner = "".join(buf)
            # ハッシュタグ・メンションは表示テキスト、それ以外のリンクは完全なURLに戻す
            if "hashtag" in cls or "mention" in cls or inner.startswith(("#", "@")):
                self._emit(inner)
            else:
                self._emit(href or inner)

    def handle_data(self, data):
        self._emit(data)

    def text(self):
        t = "".join(self.parts)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()


def html_to_text(html_str):
    if not html_str:
        return ""
    p = ContentParser()
    try:
        p.feed(html_str)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", "", html_str).strip()
    return p.text()


# ---------------------------------------------------------------
# 共通変換
# ---------------------------------------------------------------

VIS_MAP = {"public": None, "unlisted": "home",
           "private": "followers", "direct": "specified"}

KIND_MAP = {"image": "image", "gifv": "gifv", "video": "video",
            "audio": "audio", "unknown": "file"}

# Mastodon Snowflake ID: 上位48bitがUNIXエポックからのミリ秒
def snowflake_dt(status_id):
    try:
        i = int(str(status_id))
    except (TypeError, ValueError):
        return None
    if i < (1 << 16):
        return None
    ms = i >> 16
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    lo = datetime(2016, 1, 1, tzinfo=timezone.utc)
    hi = datetime.now(timezone.utc) + timedelta(days=2)
    return dt if lo <= dt <= hi else None


def compose_text(content_html, spoiler, poll_options=None):
    text = html_to_text(content_html)
    if poll_options:
        opts = " / ".join(o for o in poll_options if o)
        if opts:
            text = (text + "\n\n" if text else "") + "\U0001F4CA " + opts
    if spoiler:
        text = f"【CW】{spoiler}\n{text}"
    return text


def acct_of(account_obj, host):
    """APIのaccountオブジェクト → @user@host 形式。"""
    a = (account_obj or {}).get("acct") or (account_obj or {}).get("username") or ""
    if not a:
        return None
    return "@" + (a if "@" in a else f"{a}@{host}")


STATUS_URI_RE = re.compile(
    r"https?://([^/]+)/(?:users/([^/]+)/statuses/([A-Za-z0-9]+)"
    r"|@([^/]+)/(\d+))")


def parse_status_uri(uri):
    """投稿URI → (@user@host, status_id)。解析不能なら (None, None)。"""
    m = STATUS_URI_RE.match(uri or "")
    if not m:
        return None, None
    host = m.group(1)
    user = m.group(2) or m.group(4)
    sid = m.group(3) or m.group(5)
    return (f"@{user}@{host}" if user else None), sid


# ---------------------------------------------------------------
# メディア
# ---------------------------------------------------------------

def save_attachment(sess, host, att, skip):
    """API添付 → mediaレコード。ダウンロード成功時は local_path 付き。"""
    kind = KIND_MAP.get(att.get("type"), "file")
    url = att.get("url") or att.get("remote_url") or att.get("preview_url")
    rec = {"kind": kind, "local_path": None,
           "remote_url": url, "alt": att.get("description")}
    if not url or skip:
        return rec
    name = common.sanitize(url.split("?")[0].rsplit("/", 1)[-1], 90)
    aid = str(att.get("id") or "")
    fname = f"{aid}_{name}" if aid else name
    if "." not in fname:
        fname += common.ext_for(None, url)
    dest = common.MEDIA_DIR / f"mastodon_{host}" / fname
    if common.download(url, dest, sess, quiet=True):
        rec["local_path"] = f"mastodon_{host}/{fname}"
        time.sleep(DL_SLEEP)
    return rec


# ---------------------------------------------------------------
# A) API取得
# ---------------------------------------------------------------

class MdApi:
    def __init__(self, host, token):
        self.base = f"https://{host}"
        self.host = host
        self.sess = requests.Session()
        self.sess.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": common.USER_AGENT,
        })

    def get(self, path, params=None):
        url = path if path.startswith("http") else self.base + path
        for attempt in range(5):
            try:
                r = self.sess.get(url, params=params, timeout=60)
            except Exception as e:
                if attempt == 4:
                    raise MdError(f"接続失敗: {e}")
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"    レート制限。{wait}秒待機します…")
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise MdError(f"認証エラー(HTTP {r.status_code})。トークンを確認してください。")
            if r.status_code >= 400:
                raise MdError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r
        raise MdError("リトライ上限に達しました")


def next_link(resp):
    """Linkヘッダから rel=next のURLを取り出す。"""
    for part in (resp.headers.get("Link") or "").split(","):
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                return m.group(1)
    return None


def status_row(st, host, account, tz):
    """APIのstatus → upsert用フィールド。(fields, media_src_status)"""
    rb = st.get("reblog")
    src = rb or st
    poll = [o.get("title") for o in ((src.get("poll") or {}).get("options") or [])]
    text = compose_text(src.get("content"), src.get("spoiler_text"), poll)
    dt = common.parse_iso(st.get("created_at"))
    c_utc, c_loc = common.iso_pair(dt, tz)
    extra = {}
    vis = VIS_MAP.get(st.get("visibility"))
    if vis:
        extra["visibility"] = vis
    if rb:
        ptype = "repost"
        author = acct_of(rb.get("account"), host)
        author_name = (rb.get("account") or {}).get("display_name") or None
        url = rb.get("url") or st.get("url")
        extra["boost_date"] = True  # 日時はブーストした日時（元投稿の日時ではない）
    else:
        ptype = "post"
        author = account
        author_name = None
        url = st.get("url")
        fc, rc = st.get("favourites_count"), st.get("reblogs_count")
        if fc:
            extra["fav_count"] = fc
        if rc:
            extra["rt_count"] = rc
    fields = dict(
        post_id=str(st.get("id")), type=ptype,
        created_at=c_utc, created_local=c_loc, text=text,
        author_handle=author, author_name=author_name, url=url,
        is_reply=1 if st.get("in_reply_to_id") else 0,
        extra=extra or None)
    return fields, src


def sync_api(cfg, entry, args):
    host = entry["host"].strip().lower().replace("https://", "").strip("/")
    api = MdApi(host, entry["token"].strip())
    me = api.get("/api/v1/accounts/verify_credentials").json()
    username = me.get("username") or me.get("acct")
    account = (entry.get("label") or "").strip() or f"@{username}@{host}"
    print(f"=== Mastodon取り込み(API): {account} ===")

    con = common.connect()
    tz = common.get_tz(cfg)
    like_media = (not args.skip_like_media) and cfg.get("download_like_media", True)
    state_key = f"mastodon:{host}:{me.get('id')}"
    if args.full:
        con.execute("DELETE FROM sync_state WHERE key LIKE ?", (state_key + "%",))
        con.commit()

    # ---- 初回のみ: アーカイブがあれば先に取り込んでAPIの往復を大幅節約 ----
    if (common.get_state(con, state_key + ":contig_oldest") is None
            and (entry.get("archive") or "").strip()):
        apath = Path(entry["archive"]).expanduser()
        if not apath.exists():
            print(f"  （archive に指定されたファイルが見つかりません: {apath}）")
            print("  APIのみで初回同期を行います。")
        else:
            print("初回同期です。先にアーカイブから一括取り込みし、以降の差分をAPIで取得します。")
            con.close()
            try:
                sync_export(cfg, entry, args, account_override=account)
            except MdError as e:
                print(f"  ! アーカイブ取り込みに失敗（{e}）。APIのみで初回同期を行います。")
            con = common.connect()
            row = con.execute(
                "SELECT MIN(CAST(post_id AS INTEGER)) FROM posts "
                "WHERE service='mastodon' AND account=? AND type='post' "
                "AND post_id GLOB '[0-9]*'", (account,)).fetchone()
            if row and row[0]:
                common.set_state(con, state_key + ":contig_oldest", str(row[0]))
                common.set_state(con, state_key + ":notes_done", "1")
                common.set_state(con, state_key + ":favs_done", "1")
                con.commit()
                print(f"アーカイブ分を取り込み済みとして記録しました（ID {row[0]} まで）。"
                      "続けて新しい分をAPIで差分取得します。")

    # ---- エクスポート由来の行との重複排除用マップ ----
    # エクスポートのブースト/お気に入りは「元投稿のID」で保存されるが、
    # APIは「自分のサーバ上のID」を使うため、同じ投稿が二重になるのを防ぐ。
    def load_key_map(ptype):
        out = {}
        for r in con.execute(
                "SELECT id, post_id, url FROM posts "
                "WHERE service='mastodon' AND account=? AND type=?",
                (account, ptype)):
            m2 = re.match(r"https?://([^/]+)/", r["url"] or "")
            _, sid2 = parse_status_uri(r["url"] or "")
            if m2 and sid2:
                out[(m2.group(1).lower(), sid2)] = (r["id"], r["post_id"])
        return out

    def dedupe(key_map, origin_url, new_post_id):
        """同じ元投稿を指す旧行（ID体系違い）があれば行番号を返し、マップから除く。"""
        m2 = re.match(r"https?://([^/]+)/", origin_url or "")
        _, sid2 = parse_status_uri(origin_url or "")
        if not (m2 and sid2):
            return None
        key = (m2.group(1).lower(), sid2)
        old = key_map.get(key)
        if old and old[1] != new_post_id:
            del key_map[key]
            return old[0]
        return None

    def replace_old_row(old_row_id, new_pid):
        """旧行のブックマークを新行へ引き継いでから旧行を削除。"""
        con.execute("UPDATE OR IGNORE bookmarks SET post_id=? WHERE post_id=?",
                    (new_pid, old_row_id))
        con.execute("DELETE FROM bookmarks WHERE post_id=?", (old_row_id,))
        con.execute("DELETE FROM media WHERE post_id=?", (old_row_id,))
        con.execute("DELETE FROM posts WHERE id=?", (old_row_id,))

    boost_keys = load_key_map("repost")
    like_keys = load_key_map("like")

    # ---- 投稿・ブースト ----
    # contig_oldest: 穴なく取得済みと保証された最古ID。差分でもここまで遡って穴を埋める。
    contig_oldest = common.get_state(con, state_key + ":contig_oldest")
    # 差分モード: 過去に全ページの走査が完了していれば、以降は取り込み済みの
    # 投稿が KNOWN_STREAK 件連続した時点で遡りを打ち切る（--full で全走査）。
    diff_mode = (common.get_state(con, state_key + ":notes_done") == "1"
                 or bool(contig_oldest))     # 旧バージョンのDBとの互換
    known = (common.existing_post_ids(con, "mastodon", account, ["post", "repost"])
             if (contig_oldest or diff_mode) else set())
    diff_mode = diff_mode and bool(known)
    n_new = 0
    max_id = None
    reached_contig = False
    known_streak = 0
    stop_diff = False

    def id_lt(a, b):
        """a < b をID(数値優先)で比較。"""
        try:
            return int(a) < int(b)
        except (TypeError, ValueError):
            return str(a) < str(b)

    while True:
        params = {"limit": PAGE_LIMIT}
        if max_id:
            params["max_id"] = max_id
        try:
            page = api.get(f"/api/v1/accounts/{me['id']}/statuses", params).json()
        except MdError:
            if diff_mode:
                # 途中失敗のまま差分打ち切りを続けると取り逃すため、次回は全走査に戻す
                con.execute("DELETE FROM sync_state WHERE key=?",
                            (state_key + ":notes_done",))
                con.execute("DELETE FROM sync_state WHERE key=?",
                            (state_key + ":contig_oldest",))
                con.commit()
            raise
        if not page:
            break
        page_min = None
        for st in page:
            f, src = status_row(st, host, account, tz)
            if f["post_id"] in known:
                known_streak += 1
                if diff_mode and known_streak >= min(common.KNOWN_STREAK, len(known)):
                    stop_diff = True
            else:
                known_streak = 0
                n_new += 1
            old_row = (dedupe(boost_keys, (st.get("reblog") or {}).get("url"),
                              f["post_id"]) if st.get("reblog") else None)
            pid = common.upsert_post(con, service="mastodon", account=account, **f)
            if old_row:
                replace_old_row(old_row, pid)
            atts = src.get("media_attachments") or []
            if atts:
                items = [save_attachment(api.sess, host, a, args.skip_media)
                         for a in atts]
                common.replace_media(con, pid, items)
            sid = str(st["id"])
            if page_min is None or id_lt(sid, page_min):
                page_min = sid
            if contig_oldest and not id_lt(contig_oldest, sid):
                reached_contig = True   # sid <= contig_oldest
        con.commit()
        print(f"  投稿を取得中… +{len(page)} 件（新規 {n_new}）")
        if stop_diff:
            print(f"  取り込み済みの投稿が {common.KNOWN_STREAK} 件続いたため、"
                  "差分取得を終了します。")
            break
        if reached_contig:
            print("  取得済みの範囲に到達しました。")
            break
        prev_max = max_id
        max_id = page_min                # max_id は exclusive
        if max_id == prev_max:
            break
        time.sleep(SLEEP)
    if not diff_mode:
        row = con.execute(
            "SELECT MIN(CAST(post_id AS INTEGER)) FROM posts WHERE service='mastodon' "
            "AND account=? AND type IN ('post','repost')", (account,)).fetchone()
        if row and row[0]:
            common.set_state(con, state_key + ":contig_oldest", str(row[0]))
        common.set_state(con, state_key + ":notes_done", "1")
    con.commit()

    # ---- お気に入り ----
    known_l = common.existing_post_ids(con, "mastodon", account, ["like"])
    # 差分モード: お気に入りは「お気に入りした順」に返るので、一度全ページを
    # 走査済みなら、取り込み済みが KNOWN_STREAK 件連続した時点で打ち切れる。
    f_diff = (common.get_state(con, state_key + ":favs_done") == "1"
              or bool(contig_oldest))        # 旧バージョンのDBとの互換
    n_like = 0
    url = "/api/v1/favourites"
    params = {"limit": PAGE_LIMIT}
    seen_urls = set()
    f_streak = 0
    stop_f = False
    while url:
        if url in seen_urls:
            break
        seen_urls.add(url)
        try:
            resp = api.get(url, params)
        except MdError:
            if f_diff:
                # 途中失敗のまま差分打ち切りを続けると取り逃すため、次回は全走査に戻す
                con.execute("DELETE FROM sync_state WHERE key=?",
                            (state_key + ":favs_done",))
                con.commit()
            raise
        params = None  # 2ページ目以降はLinkヘッダのURLに全部入っている
        page = resp.json()
        if not page:
            break
        for st in page:
            sid = str(st.get("id"))
            if sid in known_l:
                f_streak += 1
                if f_diff and known_l and f_streak >= min(common.KNOWN_STREAK,
                                                          len(known_l)):
                    stop_f = True
            else:
                f_streak = 0
                n_like += 1
            src = st.get("reblog") or st
            poll = [o.get("title") for o in ((src.get("poll") or {}).get("options") or [])]
            text = compose_text(src.get("content"), src.get("spoiler_text"), poll)
            dt = common.parse_iso(st.get("created_at"))
            c_utc, c_loc = common.iso_pair(dt, tz)
            extra = {"date_estimated": True}  # お気に入りした日時ではなく元投稿の日時
            old_row = dedupe(like_keys, st.get("url"), sid)
            pid = common.upsert_post(
                con, service="mastodon", account=account, post_id=sid,
                type="like", created_at=c_utc, created_local=c_loc, text=text,
                author_handle=acct_of(st.get("account"), host),
                author_name=(st.get("account") or {}).get("display_name") or None,
                url=st.get("url"), is_reply=0, extra=extra)
            if old_row:
                replace_old_row(old_row, pid)
            atts = src.get("media_attachments") or []
            if atts:
                items = [save_attachment(api.sess, host, a,
                                         args.skip_media or not like_media)
                         for a in atts]
                common.replace_media(con, pid, items)
        con.commit()
        print(f"  お気に入りを取得中… +{len(page)} 件（新規 {n_like}）")
        if stop_f:
            print(f"  取り込み済みのお気に入りが {common.KNOWN_STREAK} 件続いたため、"
                  "差分取得を終了します。")
            break
        url = next_link(resp)
        time.sleep(SLEEP)
    common.set_state(con, state_key + ":likes_done", "1")
    con.commit()
    common.set_state(con, state_key + ":favs_done", "1")
    con.commit()
    con.close()
    print(f"---- 完了: 新規 投稿/ブースト {n_new} / お気に入り {n_like} ----")


# ---------------------------------------------------------------
# B) アーカイブ（エクスポートzip）取り込み
# ---------------------------------------------------------------

def resolve_export_dir(p: Path):
    """(outbox.jsonのあるdir, 展開ルート or None) を返す。"""
    extracted_root = None
    if p.is_file() and p.suffix.lower() == ".zip":
        out = (common.ROOT / "mastodon_archive_extracted"
               / common.sanitize(p.stem, limit=60))
        marker = out / ".extracted_ok"
        if not marker.exists():
            print(f"zipを展開しています → {out}")
            out.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(p) as z:
                z.extractall(out)
            marker.write_text("ok", encoding="utf-8")
        p = out
        extracted_root = out
    cands = [p]
    if p.is_dir():
        cands += [d for d in sorted(p.iterdir()) if d.is_dir()]
    for cand in cands:
        if (cand / "outbox.json").exists():
            return cand, extracted_root
    raise MdError(f"outbox.json が見つかりません: {p}")


def copy_export_media(export_dir: Path, host, att):
    """outboxのattachment → mediaレコード。アーカイブ内のファイルをコピー。"""
    mime = att.get("mediaType") or ""
    kind = ("image" if mime.startswith("image/") else
            "video" if mime.startswith("video/") else
            "audio" if mime.startswith("audio/") else "file")
    rel = (att.get("url") or "").lstrip("/")
    rec = {"kind": kind, "local_path": None, "remote_url": None,
           "alt": att.get("name")}
    if not rel:
        return rec
    src = export_dir / rel
    if not src.exists():
        # 一部のエクスポートは media_attachments/ 直下にフラットに置かれる
        alt_src = export_dir / "media_attachments" / Path(rel).name
        src = alt_src if alt_src.exists() else src
    if not src.exists():
        return rec
    fname = common.sanitize(Path(rel).name, 90)
    dest = common.MEDIA_DIR / f"mastodon_{host}" / fname
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if (not dest.exists()) or dest.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dest)
        rec["local_path"] = f"mastodon_{host}/{fname}"
    except Exception as e:
        print(f"    ! メディアコピー失敗 {fname}: {e}")
    return rec


def export_visibility(obj):
    PUB = "https://www.w3.org/ns/activitystreams#Public"
    to = obj.get("to") or []
    cc = obj.get("cc") or []
    if PUB in to:
        return None
    if PUB in cc:
        return "home"
    if any(str(u).endswith("/followers") for u in to):
        return "followers"
    return "specified"


def sync_export(cfg, entry, args, account_override=None):
    export_dir, extracted_root = resolve_export_dir(Path(entry["archive"]).expanduser())
    tz = common.get_tz(cfg)

    host = username = None
    actor_f = export_dir / "actor.json"
    if actor_f.exists():
        actor = json.loads(actor_f.read_text(encoding="utf-8"))
        username = actor.get("preferredUsername")
        m = re.match(r"https?://([^/]+)/", actor.get("id") or actor.get("url") or "")
        host = m.group(1) if m else None
    host = host or (entry.get("host") or "mastodon").strip()
    account = (account_override
               or (entry.get("label") or "").strip()
               or (f"@{username}@{host}" if username else f"@{host}"))
    print(f"=== Mastodon取り込み(アーカイブ): {account} ===")
    print(f"データフォルダ: {export_dir}")

    outbox = json.loads((export_dir / "outbox.json").read_text(encoding="utf-8"))
    items = outbox.get("orderedItems") or []
    con = common.connect()
    n_post = n_boost = 0

    for i, act in enumerate(items):
        atype = act.get("type")
        if atype == "Create" and isinstance(act.get("object"), dict):
            obj = act["object"]
            _, sid = parse_status_uri(obj.get("id") or "")
            sid = sid or str(obj.get("id") or "")[-40:]
            if not sid:
                continue
            poll = [o.get("name") for o in (obj.get("oneOf") or obj.get("anyOf") or [])]
            text = compose_text(obj.get("content"), obj.get("summary"), poll)
            dt = common.parse_iso(obj.get("published"))
            c_utc, c_loc = common.iso_pair(dt, tz)
            extra = {}
            vis = export_visibility(obj)
            if vis:
                extra["visibility"] = vis
            pid = common.upsert_post(
                con, service="mastodon", account=account, post_id=sid,
                type="post", created_at=c_utc, created_local=c_loc, text=text,
                author_handle=account, author_name=None,
                url=obj.get("url") or obj.get("id"),
                is_reply=1 if obj.get("inReplyTo") else 0, extra=extra or None)
            atts = obj.get("attachment") or []
            if atts and not args.skip_media:
                common.replace_media(
                    con, pid, [copy_export_media(export_dir, host, a) for a in atts])
            n_post += 1
        elif atype == "Announce" and isinstance(act.get("object"), str):
            uri = act["object"]
            author, sid = parse_status_uri(uri)
            dt = common.parse_iso(act.get("published"))
            c_utc, c_loc = common.iso_pair(dt, tz)
            common.upsert_post(
                con, service="mastodon", account=account,
                post_id=sid or uri, type="repost",
                created_at=c_utc, created_local=c_loc,
                text="（ブースト — 本文は未取得です）",
                author_handle=author, author_name=None, url=uri,
                is_reply=0, extra={"export_stub": True})
            n_boost += 1
        if (i + 1) % 1000 == 0:
            con.commit()
            print(f"  … {i + 1} 件処理")
    con.commit()

    # ---- お気に入り（likes.json はURIの一覧のみ） ----
    n_like = 0
    likes_f = export_dir / "likes.json"
    if likes_f.exists():
        likes = json.loads(likes_f.read_text(encoding="utf-8"))
        for uri in likes.get("orderedItems") or []:
            if not isinstance(uri, str):
                continue
            author, sid = parse_status_uri(uri)
            dt = snowflake_dt(sid)
            c_utc, c_loc = common.iso_pair(dt, tz)
            extra = {"export_stub": True}
            if dt:
                extra["date_estimated"] = True
            common.upsert_post(
                con, service="mastodon", account=account,
                post_id=sid or uri, type="like",
                created_at=c_utc, created_local=c_loc,
                text="（お気に入り — 本文は未取得です）",
                author_handle=author, author_name=None, url=uri,
                is_reply=0, extra=extra)
            n_like += 1
        con.commit()
    print(f"---- 取り込み完了: 投稿 {n_post} / ブースト {n_boost} / お気に入り {n_like} ----")
    like_media = (not args.skip_like_media) and cfg.get("download_like_media", True)
    if not getattr(args, "skip_enrich", False):
        enrich_export_stubs(con, cfg, account, args, like_media)
    con.close()
    print("※ アーカイブに含まれないブースト・お気に入りの本文は、"
          "元サーバの公開APIから自動補完しています。")
    print("  取得できなかった分は sync_mastodon.bat の再実行で続きから補完されます。")
    keep = getattr(args, "keep_extracted", False) or bool(cfg.get("keep_extracted"))
    common.cleanup_extracted(extracted_root, keep=keep)


# ---------------------------------------------------------------
# エクスポートで欠けた本文・メディアを元サーバの公開APIで補完
# （公開投稿は認証なしで /api/v1/statuses/:id から取得できる）
# ---------------------------------------------------------------

def fetch_public_status(sess, host, sid):
    """公開ステータスを取得。戻り値: (status | None, 'ok'|'gone'|'fail')"""
    url = f"https://{host}/api/v1/statuses/{sid}"
    for attempt in range(3):
        try:
            r = sess.get(url, timeout=45,
                         headers={"User-Agent": common.USER_AGENT})
        except Exception:
            if attempt == 2:
                return None, "fail"
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1))
            continue
        if r.status_code in (401, 403, 404, 410):
            return None, "gone"       # 削除済み or 非公開 → 公開APIでは取得不能
        if r.status_code >= 400:
            return None, "fail"
        try:
            return r.json(), "ok"
        except Exception:
            return None, "fail"
    return None, "fail"


def enrich_export_stubs(con, cfg, account, args, like_media):
    rows = con.execute(
        "SELECT id, post_id, type, url, extra FROM posts "
        "WHERE service='mastodon' AND account=? AND extra LIKE '%\"export_stub\"%'",
        (account,)).fetchall()
    if not rows:
        return
    print(f"公開APIで {len(rows)} 件の本文・メディアを補完しています…"
          "（中断しても再実行で続きから補完されます）")
    sess = requests.Session()
    host_fail = {}                     # ホストごとの連続失敗数（5回で打ち切り）
    ok = gone = fail = 0
    for i, row in enumerate(rows):
        author_uri, sid = parse_status_uri(row["url"] or "")
        m = re.match(r"https?://([^/]+)/", row["url"] or "")
        host = m.group(1) if m else None
        if not host or not sid:
            # URIから投稿IDを特定できない（Pleroma等） → 公開APIでは補完不能
            extra = json.loads(row["extra"]) if row["extra"] else {}
            extra.pop("export_stub", None)
            extra["gone"] = True
            con.execute("UPDATE posts SET extra=? WHERE id=?",
                        (json.dumps(extra, ensure_ascii=False), row["id"]))
            gone += 1
            continue
        if host_fail.get(host, 0) >= 5:
            fail += 1
            continue
        st, status = fetch_public_status(sess, host, sid)
        extra = json.loads(row["extra"]) if row["extra"] else {}
        extra.pop("export_stub", None)
        if status == "ok" and st:
            host_fail[host] = 0
            src = st.get("reblog") or st
            poll = [o.get("title") for o in ((src.get("poll") or {}).get("options") or [])]
            text = compose_text(src.get("content"), src.get("spoiler_text"), poll)
            acct = st.get("account") or {}
            con.execute(
                "UPDATE posts SET text=?, author_handle=?, author_name=?, url=?, extra=? "
                "WHERE id=?",
                (text, acct_of(acct, host), acct.get("display_name") or None,
                 st.get("url") or row["url"],
                 json.dumps(extra, ensure_ascii=False) if extra else None, row["id"]))
            atts = src.get("media_attachments") or []
            if atts:
                skip = args.skip_media or (row["type"] == "like" and not like_media)
                common.replace_media(
                    con, row["id"],
                    [save_attachment(sess, host, a, skip) for a in atts])
            ok += 1
        elif status == "gone":
            host_fail[host] = 0
            extra["gone"] = True
            cur = con.execute("SELECT text FROM posts WHERE id=?", (row["id"],)).fetchone()
            text = cur["text"]
            if "アーカイブに含まれていません" in text or "未取得" in text:
                text = "（元の投稿は削除済みか、公開範囲の制限により取得できません）"
            con.execute("UPDATE posts SET text=?, extra=? WHERE id=?",
                        (text, json.dumps(extra, ensure_ascii=False), row["id"]))
            gone += 1
        else:
            host_fail[host] = host_fail.get(host, 0) + 1
            if host_fail[host] == 5:
                print(f"  ! {host} に接続できないため、このサーバの補完をスキップします")
            fail += 1
        time.sleep(0.3)
        if (i + 1) % 50 == 0:
            con.commit()
            print(f"  … {i + 1}/{len(rows)} 件補完")
    con.commit()
    print(f"---- 補完完了: 成功 {ok} / 取得不能 {gone} / 保留 {fail} ----")


def main():
    ap = argparse.ArgumentParser(description="Mastodon取り込み")
    ap.add_argument("--full", action="store_true", help="差分ではなく全件を取得し直す")
    ap.add_argument("--skip-media", action="store_true", help="メディアを保存しない")
    ap.add_argument("--skip-like-media", action="store_true",
                    help="お気に入りのメディアはダウンロードしない")
    ap.add_argument("--skip-enrich", action="store_true",
                    help="エクスポート取り込み後の公開APIによる補完を行わない")
    ap.add_argument("--keep-extracted", action="store_true",
                    help="zipから展開した一時データを取り込み後に削除しない")
    args = ap.parse_args()

    cfg = common.load_config()
    entries = common.as_list(cfg.get("mastodon"))
    entries = [e for e in entries
               if (e.get("token") or "").strip() and (e.get("host") or "").strip()
               or (e.get("archive") or "").strip()]
    if not entries:
        print("mastodon の設定が無いためスキップします（config.json の mastodon）")
        return
    for i, entry in enumerate(entries):
        if len(entries) > 1:
            print(f"\n##### Mastodonアカウント {i + 1}/{len(entries)} #####")
        try:
            if (entry.get("token") or "").strip():
                sync_api(cfg, entry, args)
            else:
                sync_export(cfg, entry, args)
        except MdError as e:
            print(f"! Mastodonエラー: {e}")


if __name__ == "__main__":
    main()
