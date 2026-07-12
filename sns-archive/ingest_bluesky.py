# -*- coding: utf-8 -*-
"""
ingest_bluesky.py — Bluesky (AT Protocol) から自分のデータを取得する

取得するもの:
  - 自分の投稿（app.bsky.feed.post を全件 listRecords）
    画像・動画はPDSから元ファイル(BLOB)をダウンロード
  - リポスト（app.bsky.feed.repost）→ 元投稿の本文・作者・画像を取得
  - いいね（app.bsky.feed.like）→ 同上（いいねした日時つき）
  - 引用ポストは元投稿の本文を展開して検索可能にする

認証にはアプリパスワードを使用（設定 → プライバシーとセキュリティ → アプリパスワード）。
"""
import argparse
import re
import time
import zipfile
from pathlib import Path

import requests

import carfile
import common

PAGE_SLEEP = 0.3
HYDRATE_SLEEP = 0.35
DL_SLEEP = 0.1


class BskyError(Exception):
    pass


class Bsky:
    def __init__(self, pds, identifier, password):
        self.pds = pds.rstrip("/")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = common.USER_AGENT
        r = self.s.post(f"{self.pds}/xrpc/com.atproto.server.createSession",
                        json={"identifier": identifier, "password": password},
                        timeout=60)
        if r.status_code >= 400:
            raise BskyError(f"ログイン失敗 HTTP {r.status_code}: {r.text[:300]}\n"
                            "ハンドルとアプリパスワードを確認してください。")
        d = r.json()
        self.did = d["did"]
        self.handle = d.get("handle") or identifier
        self.access = d["accessJwt"]
        self.refresh = d["refreshJwt"]

    def _refresh(self):
        r = self.s.post(f"{self.pds}/xrpc/com.atproto.server.refreshSession",
                        headers={"Authorization": f"Bearer {self.refresh}"},
                        timeout=60)
        if r.status_code >= 400:
            raise BskyError(f"セッション更新失敗: {r.text[:200]}")
        d = r.json()
        self.access = d["accessJwt"]
        self.refresh = d["refreshJwt"]

    def call(self, nsid, params=None, raw=False):
        url = f"{self.pds}/xrpc/{nsid}"
        for attempt in range(5):
            try:
                r = self.s.get(url, params=params, timeout=90,
                               headers={"Authorization": f"Bearer {self.access}"})
            except Exception as e:
                if attempt == 4:
                    raise BskyError(f"{nsid}: {e}")
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (400, 401):
                err = None
                try:
                    err = r.json().get("error")
                except Exception:
                    pass
                if err == "ExpiredToken":
                    self._refresh()
                    continue
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise BskyError(f"{nsid} HTTP {r.status_code}: {r.text[:300]}")
            return r.content if raw else r.json()
        raise BskyError(f"{nsid}: リトライ上限")


def rkey(uri):
    return uri.rsplit("/", 1)[-1]


def did_of(uri):
    # at://did:plc:xxxx/collection/rkey
    m = re.match(r"^at://([^/]+)/", uri or "")
    return m.group(1) if m else None


def blob_cid(b):
    if not isinstance(b, dict):
        return None, None
    cid = None
    ref = b.get("ref")
    if isinstance(ref, dict):
        cid = ref.get("$link")
    cid = cid or b.get("cid")
    return cid, b.get("mimeType")


def expand_facets(text, facets):
    """短縮表示されたリンクをフルURLに展開する（バイトオフセット処理）。"""
    if not text or not facets:
        return text or ""
    try:
        b = text.encode("utf-8")
        repl = []
        for f in facets:
            idx = f.get("index") or {}
            for feat in f.get("features") or []:
                if feat.get("$type") == "app.bsky.richtext.facet#link" and feat.get("uri"):
                    repl.append((idx.get("byteStart", 0), idx.get("byteEnd", 0), feat["uri"]))
        def bare(u):
            return re.sub(r"^https?://", "", u or "")

        for s, e, uri in sorted(repl, key=lambda x: x[0], reverse=True):
            seg = b[s:e].decode("utf-8", "ignore").strip()
            core, truncated = seg, False
            for suf in ("…", "..."):
                if core.endswith(suf):
                    core = core[: -len(suf)]
                    truncated = True
                    break
            if truncated or (core and bare(uri).startswith(bare(core))):
                b = b[:s] + uri.encode("utf-8") + b[e:]
        return b.decode("utf-8", "ignore")
    except Exception:
        return text


def walk_embed(embed, media, quotes, extra):
    """listRecords（生レコード）内の embed からメディアBLOB・引用URIを収集。"""
    if not isinstance(embed, dict):
        return
    t = embed.get("$type", "")
    if t.startswith("app.bsky.embed.images"):
        for im in embed.get("images") or []:
            cid, mime = blob_cid(im.get("image") or {})
            if cid:
                media.append({"kind": "image", "cid": cid,
                              "mime": mime or "image/jpeg",
                              "alt": im.get("alt") or None})
    elif t.startswith("app.bsky.embed.video"):
        cid, mime = blob_cid(embed.get("video") or {})
        if cid:
            media.append({"kind": "video", "cid": cid,
                          "mime": mime or "video/mp4",
                          "alt": embed.get("alt") or None})
    elif t.startswith("app.bsky.embed.external"):
        ext = embed.get("external") or {}
        if ext.get("uri"):
            extra["external"] = {"uri": ext["uri"], "title": ext.get("title")}
    elif t.startswith("app.bsky.embed.recordWithMedia"):
        walk_embed(embed.get("media") or {}, media, quotes, extra)
        u = ((embed.get("record") or {}).get("record") or {}).get("uri")
        if u:
            quotes.append(u)
    elif t.startswith("app.bsky.embed.record"):
        u = (embed.get("record") or {}).get("uri")
        if u:
            quotes.append(u)


def view_embed_media(embed, media, extra, quote_texts):
    """getPosts の embedビュー（ハイドレート済み）からメディアURL等を収集。"""
    if not isinstance(embed, dict):
        return
    t = embed.get("$type", "")
    if t.startswith("app.bsky.embed.images"):
        for im in embed.get("images") or []:
            if im.get("fullsize"):
                media.append({"kind": "image", "remote": im["fullsize"],
                              "alt": im.get("alt") or None})
    elif t.startswith("app.bsky.embed.video"):
        media.append({"kind": "remote_video",
                      "remote": embed.get("playlist"),
                      "thumb": embed.get("thumbnail"),
                      "alt": embed.get("alt") or None})
    elif t.startswith("app.bsky.embed.external"):
        ext = embed.get("external") or {}
        if ext.get("uri"):
            extra["external"] = {"uri": ext["uri"], "title": ext.get("title")}
    elif t.startswith("app.bsky.embed.recordWithMedia"):
        view_embed_media(embed.get("media") or {}, media, extra, quote_texts)
        view_embed_media(embed.get("record") or {}, media, extra, quote_texts)
    elif t.startswith("app.bsky.embed.record"):
        rec = embed.get("record") or {}
        val = rec.get("value") or {}
        author = rec.get("author") or {}
        if val.get("text"):
            quote_texts.append((("@" + author["handle"]) if author.get("handle") else "",
                                val["text"]))


def list_all(bsky, collection, label, known=None):
    """listRecords を新しい順に列挙する。known（取り込み済みIDのset）を渡すと、
    既知レコードが KNOWN_STREAK 件連続した時点で打ち切る（差分同期）。"""
    out = []
    cursor = None
    seen_cursors = set()
    streak = 0
    while True:
        params = {"repo": bsky.did, "collection": collection, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            d = bsky.call("com.atproto.repo.listRecords", params)
        except BskyError as e:
            if not out and ("InvalidRequest" in str(e) or "Could not locate" in str(e)):
                return []  # コレクションが空
            raise
        recs = d.get("records") or []
        stop = False
        for r in recs:
            out.append(r)
            if known:
                if rkey(r.get("uri", "")) in known:
                    streak += 1
                    if streak >= min(common.KNOWN_STREAK, len(known)):
                        stop = True
                        break
                else:
                    streak = 0
        cursor = d.get("cursor")
        if recs:
            print(f"  {label} {len(out)} 件列挙…")
        if stop:
            print(f"  取り込み済みの{label}が {common.KNOWN_STREAK} 件続いたため、"
                  "差分取得を終了します。")
            break
        # 空ページ・cursor消失・同じcursorの再出現（サーバ不具合）で終了
        if not cursor or not recs or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        time.sleep(PAGE_SLEEP)
    return out


def hydrate_uris(getposts, uris):
    """URIリスト → ({uri: postView}, 取得失敗したURIのset)。25件ずつ。
    getposts(batch) は postView のリストを返す関数（失敗時 BskyError）。"""
    out, failed = {}, set()
    uris = sorted(set(u for u in uris if u))
    for i in range(0, len(uris), 25):
        batch = uris[i:i + 25]
        try:
            for p in getposts(batch):
                out[p["uri"]] = p
        except BskyError as e:
            print(f"    ! 元投稿の取得に一部失敗: {e}")
            failed.update(batch)
        if i + 25 < len(uris):
            time.sleep(HYDRATE_SLEEP)
        if (i // 25) % 20 == 19:
            print(f"  元投稿を取得中… {min(i + 25, len(uris))}/{len(uris)}")
    return out, failed


def save_remote(sess, url, skip_media, suffix=".jpg"):
    if skip_media or not url:
        return None
    m = re.search(r"/([a-z0-9]{20,})@?", url)
    base = common.sanitize(m.group(1) if m else url.rsplit("/", 1)[-1], 90)
    fname = base if "." in base else base + suffix
    dest = common.MEDIA_DIR / "bluesky" / fname
    if common.download(url, dest, sess):
        time.sleep(DL_SLEEP)
        return f"bluesky/{fname}"
    return None


def post_url(handle_or_did, uri):
    return f"https://bsky.app/profile/{handle_or_did}/post/{rkey(uri)}"


def _save_blob_bytes(cid, mime, fetch):
    """fetch() でBLOBバイト列を取得して media/bluesky/ に保存。"""
    fname = f"{common.sanitize(cid, 90)}{common.ext_for(mime)}"
    dest = common.MEDIA_DIR / "bluesky" / fname
    if dest.exists() and dest.stat().st_size > 0:
        return f"bluesky/{fname}"
    data = fetch()
    if not data:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    time.sleep(DL_SLEEP)
    return f"bluesky/{fname}"


# ---------------------------------------------------------------
# 取得元の抽象化: APIログイン / エクスポート(.car) の2系統
# ---------------------------------------------------------------

class ApiSource:
    """アプリパスワードでログインして取得（推奨）。"""
    kind = "api"

    def __init__(self, bs):
        handle = (bs.get("handle") or "").strip().lstrip("@")
        pw = (bs.get("app_password") or "").strip()
        pds = bs.get("pds") or "https://bsky.social"
        self.bsky = Bsky(pds, handle, pw)
        self.did = self.bsky.did
        self.handle = self.bsky.handle

    def list(self, collection, label, known=None):
        return list_all(self.bsky, collection, label, known=known)

    def blob(self, cid, mime, skip):
        if skip or not cid:
            return None
        try:
            return _save_blob_bytes(
                cid, mime,
                lambda: self.bsky.call("com.atproto.sync.getBlob",
                                       {"did": self.did, "cid": cid}, raw=True))
        except BskyError as e:
            print(f"    ! メディア取得失敗 {cid}: {e}")
            return None

    def hydrate(self, uris):
        return hydrate_uris(
            lambda batch: self.bsky.call(
                "app.bsky.feed.getPosts",
                [("uris", u) for u in batch]).get("posts") or [],
            uris)


PUBLIC_APPVIEW = "https://public.api.bsky.app"


def _public_get(sess, base, nsid, params, raw=False):
    """認証不要の公開XRPCエンドポイントをGET。"""
    url = f"{base}/xrpc/{nsid}"
    for attempt in range(4):
        try:
            r = sess.get(url, params=params, timeout=90,
                         headers={"User-Agent": common.USER_AGENT})
        except Exception as e:
            if attempt == 3:
                raise BskyError(f"{nsid}: {e}")
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise BskyError(f"{nsid} HTTP {r.status_code}: {r.text[:200]}")
        return r.content if raw else r.json()
    raise BskyError(f"{nsid}: リトライ上限")


def resolve_did(did, sess):
    """DID → (PDSのURL, ハンドル)。公開ディレクトリを参照（認証不要）。"""
    try:
        if did.startswith("did:plc:"):
            r = sess.get(f"https://plc.directory/{did}", timeout=30,
                         headers={"User-Agent": common.USER_AGENT})
        elif did.startswith("did:web:"):
            dom = did.split(":", 2)[2]
            r = sess.get(f"https://{dom}/.well-known/did.json", timeout=30)
        else:
            return None, None
        if r.status_code >= 400:
            return None, None
        doc = r.json()
        pds = None
        for s in doc.get("service") or []:
            if (s.get("type") == "AtprotoPersonalDataServer"
                    or str(s.get("id", "")).endswith("atproto_pds")):
                pds = (s.get("serviceEndpoint") or "").rstrip("/")
        handle = None
        for aka in doc.get("alsoKnownAs") or []:
            if aka.startswith("at://"):
                handle = aka[5:]
                break
        return pds or None, handle
    except Exception:
        return None, None


def find_car(path: Path):
    """.car / .carを含むzip / .carを含むフォルダ を解決。(.carパス, 展開ルート or None)。"""
    extracted_root = None
    if path.is_file() and path.suffix.lower() == ".car":
        return path, None
    if path.is_file() and path.suffix.lower() == ".zip":
        out = (common.ROOT / "bluesky_archive_extracted"
               / common.sanitize(path.stem, limit=60))
        marker = out / ".extracted_ok"
        if not marker.exists():
            print(f"zipを展開しています → {out}")
            out.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path) as z:
                z.extractall(out)
            marker.write_text("ok", encoding="utf-8")
        path = out
        extracted_root = out
    if path.is_dir():
        cars = sorted(path.rglob("*.car"), key=lambda f: -f.stat().st_size)
        if cars:
            return cars[0], extracted_root
    raise BskyError(f".car ファイルが見つかりません: {path}\n"
                    "Blueskyの 設定 → アカウント → アカウントデータのエクスポート で"
                    "ダウンロードした .car ファイル（またはそれを含むzip）を指定してください。")


class ExportSource:
    """公式エクスポート(.car)から読み込み。本文・メディアは公開APIで補完（認証不要）。"""
    kind = "export"

    def __init__(self, entry, sess):
        car_path, self.extracted_root = find_car(Path(entry["archive"]).expanduser())
        print(f"CARファイルを読み込んでいます: {car_path.name}"
              f" ({car_path.stat().st_size // 1024:,} KB)")
        self.did, records = carfile.repo_records(car_path.read_bytes())
        self.by_coll = {}
        for coll, rk, val in records:
            self.by_coll.setdefault(coll, []).append((rk, val))
        self.sess = sess
        self.blob_fail = 0
        pds, handle = resolve_did(self.did, sess)
        self.pds = pds or "https://bsky.social"
        self.handle = handle or (entry.get("handle") or "").strip().lstrip("@") or self.did
        if not pds and not handle:
            print("  （DIDの解決に失敗。オフラインまたはアカウント削除済みの可能性。"
                  "本文・メディアの補完はスキップされることがあります）")

    def list(self, collection, label, known=None):
        recs = [{"uri": f"at://{self.did}/{collection}/{rk}", "value": v}
                for rk, v in self.by_coll.get(collection, [])]
        print(f"  {label}: CARから {len(recs)} 件")
        return recs

    def blob(self, cid, mime, skip):
        if skip or not cid or self.blob_fail >= 10:
            return None
        try:
            local = _save_blob_bytes(
                cid, mime,
                lambda: _public_get(self.sess, self.pds, "com.atproto.sync.getBlob",
                                    {"did": self.did, "cid": cid}, raw=True))
            if local:
                self.blob_fail = 0
            return local
        except BskyError as e:
            self.blob_fail += 1
            if self.blob_fail == 10:
                print("    ! メディア取得の失敗が続くため、以降はスキップします"
                      "（再実行すると続きから補完を試みます）")
            else:
                print(f"    ! メディア取得失敗 {cid}: {e}")
            return None

    def hydrate(self, uris):
        return hydrate_uris(
            lambda batch: _public_get(
                self.sess, PUBLIC_APPVIEW, "app.bsky.feed.getPosts",
                [("uris", u) for u in batch]).get("posts") or [],
            uris)


# ---------------------------------------------------------------
# 同期本体（API / エクスポート共通）
# ---------------------------------------------------------------

def sync_all(cfg, args):
    entries = []
    for b in common.as_list(cfg.get("bluesky")):
        if (b.get("handle") or "").strip() and (b.get("app_password") or "").strip():
            entries.append(("api", b))
        elif (b.get("archive") or "").strip():
            entries.append(("export", b))
    if not entries:
        print("bluesky の設定が無いためスキップします（config.json の bluesky）")
        return
    for i, (mode, bs) in enumerate(entries):
        if len(entries) > 1:
            print(f"\n##### Blueskyアカウント {i + 1}/{len(entries)} #####")
        src = None
        seen_key = seen_sig = None
        if mode != "api":
            # 同じCARアーカイブは2回目以降スキップ（差し替え・更新で自動再取り込み、--fullで強制）
            ap_path = Path(bs.get("archive")).expanduser()
            if ap_path.is_file():
                st = ap_path.stat()
                seen_key = f"bluesky:archive_seen:{ap_path.resolve()}"
                seen_sig = f"{st.st_size},{st.st_mtime_ns}"
                if not getattr(args, "full", False):
                    c0 = common.connect()
                    seen = common.get_state(c0, seen_key)
                    c0.close()
                    if seen == seen_sig:
                        print(f"=== Bluesky取り込み(エクスポート): {ap_path.name} ===")
                        print("  このアーカイブは取り込み済みのためスキップしました。"
                              "（強制的に読み直す場合は --full）")
                        continue
        try:
            sess = requests.Session()
            src = ApiSource(bs) if mode == "api" else ExportSource(bs, sess)
            run_sync(cfg, src, args, sess)
            if seen_key and seen_sig:
                c0 = common.connect()
                common.set_state(c0, seen_key, seen_sig)
                c0.commit()
                c0.close()
            root = getattr(src, "extracted_root", None)
            keep = args.keep_extracted or bool(cfg.get("keep_extracted"))
            common.cleanup_extracted(root, keep=keep)
        except (BskyError, carfile.CarError, OSError) as e:
            print(f"! Blueskyエラー ({bs.get('handle') or bs.get('archive')}): {e}")
            # 途中失敗のまま差分打ち切りを続けると取り逃すため、次回は全件を確認する
            if src is not None and getattr(src, "kind", "") == "api":
                try:
                    c2 = common.connect()
                    c2.execute("DELETE FROM sync_state WHERE key=?",
                               (f"bluesky:{src.did}:done",))
                    c2.commit()
                    c2.close()
                    print("  ※ 同期が途中で失敗したため、次回は全件を確認します。")
                except Exception:
                    pass


def run_sync(cfg, src, args, sess):
    tz = common.get_tz(cfg)
    account = "@" + src.handle
    label = "API" if src.kind == "api" else "エクスポート"
    print(f"=== Bluesky取り込み({label}): {account} ===")
    con = common.connect()

    # 差分モード: 過去に全レコードの列挙が完了していれば、以降は取り込み済みが
    # KNOWN_STREAK 件連続した時点で列挙を打ち切る（--full で全走査）。
    state_key = f"bluesky:{src.did}:done"
    if getattr(args, "full", False):
        con.execute("DELETE FROM sync_state WHERE key=?", (state_key,))
        con.commit()
    diff_mode = (src.kind == "api"
                 and common.get_state(con, state_key) == "1")

    def known_of(types_):
        rows = con.execute(
            "SELECT post_id FROM posts WHERE service='bluesky' AND account=? "
            "AND type IN (%s) AND (extra IS NULL OR extra NOT LIKE '%%\"export_stub\"%%')"
            % ",".join("?" * len(types_)), (account, *types_)).fetchall()
        return {r[0] for r in rows}

    # ---------- 自分の投稿 ----------
    recs = src.list("app.bsky.feed.post", "投稿",
                    known=known_of(["post"]) if diff_mode else None)
    pending = []
    quote_uris = []
    for r in recs:
        v = r.get("value") or {}
        media, quotes, extra = [], [], {}
        walk_embed(v.get("embed"), media, quotes, extra)
        quote_uris += quotes
        pending.append((r, v, media, quotes, extra))
    qmap, _qfail = src.hydrate(quote_uris) if quote_uris else ({}, set())

    n_post = 0
    for r, v, media, quotes, extra in pending:
        uri = r["uri"]
        text = expand_facets(v.get("text", ""), v.get("facets"))
        for qu in quotes:
            qv = qmap.get(qu)
            if qv:
                qa = "@" + (qv.get("author") or {}).get("handle", "?")
                qt = (qv.get("record") or {}).get("text", "")
                text += f"\n\n【QT】{qa}: {qt}"
                extra["quote_of"] = post_url((qv.get("author") or {}).get("handle")
                                             or did_of(qu), qu)
            else:
                text += "\n\n【QT】（元投稿は削除済みか取得できません）"
        if extra.get("external") and extra["external"]["uri"] not in text:
            text += f"\n🔗 {extra['external']['uri']}"
        dt = common.parse_iso(v.get("createdAt"))
        c_utc, c_loc = common.iso_pair(dt, tz)
        pid = common.upsert_post(
            con, service="bluesky", account=account, post_id=rkey(uri),
            type="post", created_at=c_utc, created_local=c_loc,
            text=text, author_handle=account, author_name=None,
            url=post_url(src.handle, uri),
            is_reply=1 if v.get("reply") else 0, extra=extra or None)
        items = []
        for m in media:
            local = src.blob(m.get("cid"), m.get("mime"), args.skip_media)
            items.append({"kind": m["kind"], "local_path": local,
                          "remote_url": None, "alt": m.get("alt")})
        common.replace_media(con, pid, items)
        n_post += 1
        if n_post % 200 == 0:
            con.commit()
            print(f"  投稿 {n_post}/{len(pending)} 件処理…")
    con.commit()

    # ---------- リポスト / いいね ----------
    def known_ids(ptype):
        """取り込み済みID（ただし補完待ちのスタブは除く＝再試行対象にする）"""
        rows = con.execute(
            "SELECT post_id FROM posts WHERE service='bluesky' AND account=? "
            "AND type=? AND (extra IS NULL OR extra NOT LIKE '%\"export_stub\"%')",
            (account, ptype)).fetchall()
        return {r[0] for r in rows}

    def subjects(collection, ptype, label2, dl_media):
        known = known_ids(ptype)
        recs2 = src.list(collection, label2, known=known if diff_mode else None)
        new = [r for r in recs2 if rkey(r["uri"]) not in known]
        if not new:
            print(f"  {label2}: 全{len(recs2)}件（新規なし）")
            return len(recs2), 0
        print(f"  {label2}: 全{len(recs2)}件（{len(new)} 件の本文を取得）")
        uris = [((r.get("value") or {}).get("subject") or {}).get("uri") for r in new]
        vmap, vfail = src.hydrate(uris)
        count = 0
        for r in new:
            v = r.get("value") or {}
            suri = (v.get("subject") or {}).get("uri")
            if not suri:
                continue
            dt = common.parse_iso(v.get("createdAt"))
            c_utc, c_loc = common.iso_pair(dt, tz)
            view = vmap.get(suri)
            media_items = []
            extra = {}
            if view:
                author = view.get("author") or {}
                a_handle = "@" + author.get("handle", "?")
                a_name = author.get("displayName") or None
                vrec = view.get("record") or {}
                text = expand_facets(vrec.get("text", ""), vrec.get("facets"))
                qts = []
                vm = []
                view_embed_media(view.get("embed") or {}, vm, extra, qts)
                for qa, qt in qts:
                    text += f"\n\n【QT】{qa}: {qt}"
                for m in vm:
                    local = None
                    if m["kind"] == "image":
                        local = save_remote(sess, m.get("remote"), not dl_media)
                    elif m["kind"] == "remote_video":
                        local = save_remote(sess, m.get("thumb"), not dl_media)
                    media_items.append({"kind": m["kind"], "local_path": local,
                                        "remote_url": m.get("remote"),
                                        "alt": m.get("alt")})
                url = post_url(author.get("handle") or did_of(suri), suri)
            elif suri in vfail:
                # 通信エラー等 → スタブとして保存し、次回実行時に再試行
                a_handle, a_name = None, None
                text = "（元の投稿を取得できていません — 同期を再実行すると補完を試みます）"
                url = post_url(did_of(suri) or "unknown", suri)
                extra["export_stub"] = True
            else:
                # getPostsは成功したが含まれなかった → 削除済み（再試行しない）
                a_handle, a_name = None, None
                text = "（元の投稿は削除済みか取得できませんでした）"
                url = post_url(did_of(suri) or "unknown", suri)
            pid = common.upsert_post(
                con, service="bluesky", account=account, post_id=rkey(r["uri"]),
                type=ptype, created_at=c_utc, created_local=c_loc,
                text=text, author_handle=a_handle, author_name=a_name,
                url=url, is_reply=0, extra=extra or None)
            common.replace_media(con, pid, media_items)
            count += 1
            if count % 200 == 0:
                con.commit()
        con.commit()
        return len(recs2), count

    total_rt, _ = subjects("app.bsky.feed.repost", "repost", "リポスト",
                           not args.skip_media)
    dl_likes = (not args.skip_media) and (not args.skip_like_media) \
        and cfg.get("download_like_media", True)
    total_like, _ = subjects("app.bsky.feed.like", "like", "いいね", dl_likes)

    if src.kind == "api":
        common.set_state(con, state_key, "1")
        con.commit()
    con.close()
    print(f"---- 完了: 投稿 {n_post} / リポスト {total_rt} / いいね {total_like} ----")
    if src.kind == "export":
        print("※ エクスポート取り込みでも、本文・画像は公開APIから自動補完しています。")
        print("  取得に失敗した分は、sync_bluesky.bat を再実行すると続きから補完されます。")


def main():
    ap = argparse.ArgumentParser(description="Bluesky取り込み")
    ap.add_argument("--full", action="store_true", help="差分ではなく全件を取得し直す")
    ap.add_argument("--skip-media", action="store_true", help="メディアをダウンロードしない")
    ap.add_argument("--skip-like-media", action="store_true",
                    help="いいねした投稿のメディアはダウンロードしない")
    ap.add_argument("--keep-extracted", action="store_true",
                    help="zipから展開した一時データを取り込み後に削除しない")
    args = ap.parse_args()
    cfg = common.load_config()
    try:
        sync_all(cfg, args)
    except BskyError as e:
        print(f"! Blueskyエラー: {e}")


if __name__ == "__main__":
    main()
