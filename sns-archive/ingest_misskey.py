# -*- coding: utf-8 -*-
"""
ingest_misskey.py — Misskeyサーバから自分の投稿を自動取得する

推奨: APIモード（config.json の misskey にホスト名とアクセストークンを記載）
  - ノート（返信・リノート・チャンネル投稿含む）を全ページ取得
  - リアクション履歴（いいね）を取得
  - 添付メディアを media/misskey_<host>/ にダウンロード
  - 2回目以降は差分だけ取得（新しいノートに到達した時点で打ち切り）

フォールバック: 手動エクスポートJSON
  python ingest_misskey.py --from-export notes.json --host misskey.example --user yourname
  ※ エクスポートにはリノート本文やリアクション履歴が含まれないため、APIモード推奨。
"""
import argparse
import json
import zipfile
import sys
import time
from pathlib import Path

import requests

import common

PAGE_SLEEP = 0.7
DL_SLEEP = 0.15
_TIME2000 = 946684800000
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _plausible_ms(ms):
    """2005年〜（未来2日）に収まるミリ秒なら返す。"""
    if ms is None:
        return None
    lo = 1104537600000                       # 2005-01-01
    hi = int(time.time() * 1000) + 2 * 86400000
    return ms if lo <= ms <= hi else None


def _id_to_ms(note_id):
    """MisskeyのID（aid/aidx/ulid/meid等）から作成ミリ秒を推定する。
    インスタンスによりID体系が異なるので複数方式を試し、妥当な範囲の解を採る。
    復元できなければ None（呼び出し側は日付ウィンドウを普通に減らすので安全）。"""
    if not note_id:
        return None
    s = str(note_id)
    # aid / aidx: 先頭8文字が base36 のミリ秒(2000年基準)
    if len(s) >= 8:
        try:
            v = _plausible_ms(int(s[:8], 36) + _TIME2000)
            if v:
                return v
        except ValueError:
            pass
    # ulid: 先頭10文字が Crockford base32 の48bitミリ秒
    if len(s) >= 10:
        try:
            v = 0
            for c in s[:10].upper():
                v = v * 32 + _CROCKFORD.index(c)
            if _plausible_ms(v):
                return v
        except ValueError:
            pass
    # meid / objectid: 24桁hex 先頭8桁が秒
    if len(s) == 24:
        try:
            v = _plausible_ms(int(s[:8], 16) * 1000)
            if v:
                return v
        except ValueError:
            pass
    return None


def _fmt_ms(ms):
    if not ms:
        return "?"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ms / 1000))
    except (OverflowError, OSError, ValueError):
        return "?"


def _account_created_ms(me):
    """自分のアカウント作成日時(ミリ秒)。取れなければ Misskey 初期の 2018 を下限に。"""
    dt = common.parse_iso((me or {}).get("createdAt"))
    if dt:
        return int(dt.timestamp() * 1000)
    return 1514764800000                     # 2018-01-01


class MkError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def api(sess, host, ep, payload, token):
    url = f"https://{host}/api/{ep}"
    body = dict(payload)
    if token:
        body["i"] = token
    for attempt in range(6):
        try:
            r = sess.post(url, json=body, timeout=60,
                          headers={"User-Agent": common.USER_AGENT})
        except Exception as e:
            if attempt == 5:
                raise MkError(0, str(e))
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            wait = 8 * (attempt + 1)
            print(f"    レート制限に到達。{wait}秒待機します…")
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            raise MkError(r.status_code, r.text[:300])
        return r.json() if r.text.strip() else None
    raise MkError(0, "リトライ上限に達しました")


def handle_of(user, local_host):
    if not user:
        return None, None
    name = "@" + (user.get("username") or "?")
    if user.get("host"):
        name += "@" + user["host"]
    return name, (user.get("name") or None)


def compose_text(note):
    """CW・本文・投票をひとつの検索可能テキストにまとめる。"""
    parts = []
    if note.get("cw"):
        parts.append(f"【CW】{note['cw']}")
    if note.get("text"):
        parts.append(note["text"])
    poll = note.get("poll")
    if poll and poll.get("choices"):
        try:
            choices = " / ".join(c.get("text", "") for c in poll["choices"])
            parts.append(f"📊 {choices}")
        except Exception:
            pass
    return "\n".join(parts)


def note_media_items(note, host, dl):
    """noteのfilesをダウンロードして mediaレコードのリストを返す。dl=Noneならスキップ。"""
    items = []
    for f in note.get("files") or []:
        mime = f.get("type") or ""
        if mime.startswith("image/"):
            kind = "image"
        elif mime.startswith("video/"):
            kind = "video"
        elif mime.startswith("audio/"):
            kind = "audio"
        else:
            kind = "file"
        remote = f.get("url") or f.get("thumbnailUrl")
        fid = f.get("id") or "x"
        base = common.sanitize(f.get("name") or "", 40)
        if "." not in base:
            base += common.ext_for(mime, f.get("name"))
        fname = f"{fid}_{base}"
        local = None
        if remote and dl:
            dest = common.MEDIA_DIR / f"misskey_{host}" / fname
            if dl(remote, dest):
                local = f"misskey_{host}/{fname}"
                time.sleep(DL_SLEEP)
        items.append({"kind": kind, "local_path": local,
                      "remote_url": remote, "alt": f.get("comment")})
    return items


def is_pure_renote(note):
    return bool(note.get("renote")) and note.get("text") is None \
        and not note.get("cw") and not (note.get("fileIds") or note.get("files"))


def ingest_note(con, tz, host, account, note, dl):
    """1ノートをDBへ。戻り値: 'post' | 'repost'"""
    dt = common.parse_iso(note.get("createdAt"))
    c_utc, c_loc = common.iso_pair(dt, tz)
    url = f"https://{host}/notes/{note.get('id')}"
    extra = {}
    vis = note.get("visibility")
    if vis and vis != "public":
        extra["visibility"] = vis

    if is_pure_renote(note):
        target = note["renote"]
        a_handle, a_name = handle_of(target.get("user"), host)
        extra["renote_of"] = f"https://{host}/notes/{target.get('id')}"
        pid = common.upsert_post(
            con, service="misskey", account=account, post_id=note["id"],
            type="repost", created_at=c_utc, created_local=c_loc,
            text=compose_text(target), author_handle=a_handle,
            author_name=a_name, url=url, is_reply=0, extra=extra or None)
        common.replace_media(con, pid, note_media_items(target, host, dl))
        return "repost"

    text = compose_text(note)
    media = note_media_items(note, host, dl)
    if note.get("renote"):  # 引用リノート
        target = note["renote"]
        a_handle, _ = handle_of(target.get("user"), host)
        qt = compose_text(target)
        text += f"\n\n【RN】{a_handle}: {qt}" if qt else f"\n\n【RN】{a_handle} の投稿"
        extra["quote_of"] = f"https://{host}/notes/{target.get('id')}"
        media += note_media_items(target, host, dl)
    pid = common.upsert_post(
        con, service="misskey", account=account, post_id=note["id"],
        type="post", created_at=c_utc, created_local=c_loc,
        text=text, author_handle=account, author_name=None,
        url=url, is_reply=1 if note.get("replyId") else 0, extra=extra or None)
    common.replace_media(con, pid, media)
    return "post"


def ingest_reaction(con, tz, host, account, item, dl):
    note = item.get("note") or {}
    if not note.get("id"):
        return False
    dt = common.parse_iso(item.get("createdAt"))
    c_utc, c_loc = common.iso_pair(dt, tz)
    a_handle, a_name = handle_of(note.get("user"), host)
    extra = {"reaction": item.get("type")}
    pid = common.upsert_post(
        con, service="misskey", account=account, post_id=note["id"],
        type="like", created_at=c_utc, created_local=c_loc,
        text=compose_text(note), author_handle=a_handle, author_name=a_name,
        url=f"https://{host}/notes/{note['id']}", is_reply=0, extra=extra)
    common.replace_media(con, pid, note_media_items(note, host, dl))
    return True


# ---------------- APIモード ----------------

FLAG_SETS = [
    {"withReplies": True, "withRenotes": True, "withChannelNotes": True},
    {"withReplies": True, "withRenotes": True},
    {"includeReplies": True, "includeMyRenotes": True},
    {},
]


def pick_flags(sess, host, token, uid):
    for fs in FLAG_SETS:
        try:
            api(sess, host, "users/notes", {"userId": uid, "limit": 1, **fs}, token)
            return fs
        except MkError as e:
            if e.status == 400:
                continue
            raise
    return {}


def sync_server(cfg, server, tz, skip_media, skip_like_media):
    host = (server.get("host") or "").strip().replace("https://", "").rstrip("/")
    token = (server.get("token") or "").strip()
    if not host or not token:
        print(f"設定不足のためスキップ: {server}")
        return
    sess = requests.Session()
    me = api(sess, host, "i", {}, token)
    uid = me["id"]
    account = (server.get("label") or "").strip() or f"@{me.get('username')}@{host}"
    print(f"=== Misskey取り込み: {account} ({host}) ===")

    con = common.connect()
    dl = (None if skip_media
          else (lambda url, dest: common.download(url, dest, sess)))

    # ---- ノート ----
    flags = pick_flags(sess, host, token, uid)
    # contiguous_oldest: ここまでは穴なく取得済みと保証されている最古ID。
    contiguous_oldest = common.get_state(con, f"misskey:{host}:{uid}:contig_oldest")

    # ---- 初回のみ: アーカイブがあれば先に取り込んでAPIの往復を大幅節約 ----
    if contiguous_oldest is None and (server.get("archive") or "").strip():
        apath = Path(server["archive"]).expanduser()
        if not apath.exists():
            print(f"  （archive に指定されたファイルが見つかりません: {apath}）")
            print("  APIのみで初回同期を行います。")
        else:
            print("初回同期です。先にアーカイブから一括取り込みし、以降の差分をAPIで取得します。")
            con.close()
            try:
                from_export(server["archive"], host, me.get("username"), cfg,
                            skip_media=skip_media,
                            keep_extracted=bool(cfg.get("keep_extracted")),
                            account_override=account, token=token)
            except MkError as e:
                print(f"  ! アーカイブ取り込みに失敗（{e}）。APIのみで初回同期を行います。")
            con = common.connect()
            dl = (None if skip_media
                  else (lambda url, dest: common.download(url, dest, sess)))
            row = con.execute(
                "SELECT MIN(post_id) FROM posts WHERE service='misskey' "
                "AND account=? AND type IN ('post','repost')", (account,)).fetchone()
            if row and row[0]:
                common.set_state(con, f"misskey:{host}:{uid}:contig_oldest", row[0])
                common.set_state(con, f"misskey:{host}:{uid}:notes_done", "1")
                con.commit()
                contiguous_oldest = row[0]
                print(f"アーカイブ分を取り込み済みとして記録しました（ID {row[0]} まで）。"
                      "続けて新しい分をAPIで差分取得します。")

    # 差分モード: 過去に全期間の走査が一度完了していれば、以降は
    # 「取り込み済みノートが KNOWN_STREAK 件連続した時点」で遡りを打ち切る。
    # （全部を取り直したいときは --full。過去日時へのインポート等はそちらで拾う）
    notes_done = common.get_state(con, f"misskey:{host}:{uid}:notes_done") == "1"
    diff_mode = notes_done
    known = (common.existing_post_ids(con, "misskey", account, ["post", "repost"])
             if (contiguous_oldest or diff_mode) else set())
    diff_mode = diff_mode and bool(known)
    n = {"post": 0, "repost": 0}
    n_new = fetched = 0
    known_streak = 0
    stop_diff = False
    win_failed = False        # 全走査中にウィンドウ取得失敗があったか

    def handle_note(note):
        nonlocal n_new, fetched, known_streak, stop_diff
        if note["id"] in known:
            known_streak += 1
            # 既知の総数が少ないアカウントでは、その全件を確認できた時点で打ち切る
            if diff_mode and known_streak >= min(common.KNOWN_STREAK, len(known)):
                stop_diff = True
        else:
            known_streak = 0
            n_new += 1
        kind = ingest_note(con, tz, host, account, note, dl)
        n[kind] += 1
        fetched += 1

    # 「日付ウィンドウ」でさかのぼる。untilId だけで青天井に深追いすると、
    # 投稿数が多いサーバでは users/notes のDBクエリがステートメントタイムアウトを
    # 起こし、部分的な結果や空配列が返って途中の期間がごっそり欠けることがある。
    # 期間を区切ると createdAt インデックスで範囲シークでき、各クエリが軽くなるため
    # タイムアウトを避けられ、あるウィンドウが失敗しても古い側は独立に取得できる。
    WINDOW_MS = 60 * 24 * 60 * 60 * 1000       # 60日
    now_ms = int(time.time() * 1000)
    until_date = now_ms + 86400000             # 1日先から開始（予約投稿等の取りこぼし防止）
    contig_dt = _id_to_ms(contiguous_oldest) if contiguous_oldest else None
    reached_contig = False
    # 差分モードの下限: 取り込み済みの最新ノートの時期（+1ウィンドウの余裕）より
    # 古い期間は前回までに走査済みなので確認不要。
    diff_floor = None
    if diff_mode:
        row = con.execute(
            "SELECT MAX(post_id) FROM posts WHERE service='misskey' AND account=? "
            "AND type IN ('post','repost')", (account,)).fetchone()
        newest_ms = _id_to_ms(row[0]) if row and row[0] else None
        if newest_ms:
            diff_floor = newest_ms - WINDOW_MS

    while True:
        since_date = until_date - WINDOW_MS
        # このウィンドウ内を untilId でページング
        win_until_id = None
        win_count = 0
        while True:
            body = {"userId": uid, "limit": 100,
                    "sinceDate": max(since_date, 0), "untilDate": until_date, **flags}
            if win_until_id:
                body["untilId"] = win_until_id
            try:
                notes = api(sess, host, "users/notes", body, token) or []
            except MkError as e:
                # このウィンドウが重すぎて失敗しても、致命的にせず次の古い期間へ進む
                print(f"    ! {_fmt_ms(since_date)}〜{_fmt_ms(until_date)} の取得に失敗"
                      f"（{e}）。この期間は後で再実行時に補完されます。")
                win_failed = True
                notes = []
                break
            if not notes:
                break
            for note in notes:
                handle_note(note)
                if contiguous_oldest and note["id"] <= contiguous_oldest:
                    reached_contig = True
            win_count += len(notes)
            win_until_id = min(note["id"] for note in notes)
            con.commit()
            if reached_contig or stop_diff:
                break
            time.sleep(PAGE_SLEEP)
        if win_count:
            print(f"  ノート {fetched} 件取得（新規 {n_new}）"
                  f"… 〜{_fmt_ms(since_date)}")
        con.commit()
        if stop_diff:
            print(f"  取り込み済みのノートが {common.KNOWN_STREAK} 件続いたため、"
                  "差分取得を終了します。")
            break
        if reached_contig:
            print("  取得済みの範囲に到達しました。")
            break
        if diff_mode and diff_floor and until_date <= diff_floor:
            print("  前回取得済みの最新の時期まで確認したため、差分取得を終了します。")
            break
        # 保証済み最古の日時を下回るところまで来たら終了（差分同期の早期終了）
        if contig_dt and until_date <= contig_dt:
            break
        # アカウント作成日時より前まで走査したら終了。
        # 投稿の無い期間が長く続いても打ち切らない（休止期間の先にある
        # 古い投稿を取り逃して「取得済み」と誤記録するのを防ぐ。
        # 空ウィンドウはAPI1回/60日と軽いので、作成日まで確実に遡る）。
        if until_date <= _account_created_ms(me):
            break
        until_date = since_date                # 次はさらに古いウィンドウへ
        time.sleep(PAGE_SLEEP)

    if diff_mode:
        if win_failed:
            # 差分取得の途中で失敗すると「どこまで確認済みか」が曖昧になり、
            # 次回の既知連続打ち切りが取り逃しを生む。安全のため次回は全走査に戻す。
            con.execute("DELETE FROM sync_state WHERE key=?",
                        (f"misskey:{host}:{uid}:notes_done",))
            print("  ※ 取得に失敗した期間があるため、次回は全期間を確認します。")
    elif win_failed:
        print("  ※ 一部の期間が取得できなかったため、次回も全期間を確認します"
              "（再実行すると失敗した期間が補完されます）。")
    else:
        # 全区間を穴なく走査し終えたので、完了マークと保証済み最古を記録。
        # 以降の同期は差分モード（既知ノート連続で打ち切り）になる。
        row = con.execute(
            "SELECT MIN(post_id) FROM posts WHERE service='misskey' AND account=? "
            "AND type IN ('post','repost')", (account,)).fetchone()
        if row and row[0]:
            common.set_state(con, f"misskey:{host}:{uid}:contig_oldest", row[0])
        common.set_state(con, f"misskey:{host}:{uid}:notes_done", "1")
    con.commit()

    # ---- リアクション（いいね） ----
    n_like = 0
    like_dl = None if (skip_media or skip_like_media) else dl
    # 差分モード: 一度全ページを走査し終えていれば、以降は取り込み済みの
    # リアクション先ノートが KNOWN_STREAK 件連続した時点で打ち切る。
    r_diff = common.get_state(con, f"misskey:{host}:{uid}:reactions_done") == "1"
    known_l = (common.existing_post_ids(con, "misskey", account, ["like"])
               if r_diff else set())
    until = None
    r_streak = 0
    stop_r = False
    try:
        while True:
            body = {"userId": uid, "limit": 100}
            if until:
                body["untilId"] = until
            items = api(sess, host, "users/reactions", body, token) or []
            if not items:
                break
            for item in items:
                nid = (item.get("note") or {}).get("id")
                if nid and nid in known_l:
                    r_streak += 1
                    if r_diff and known_l and r_streak >= min(common.KNOWN_STREAK,
                                                              len(known_l)):
                        stop_r = True
                else:
                    r_streak = 0
                if ingest_reaction(con, tz, host, account, item, like_dl):
                    n_like += 1
            con.commit()
            print(f"  リアクション {n_like} 件取得…")
            if stop_r:
                print(f"  取り込み済みのリアクションが {common.KNOWN_STREAK} 件続いたため、"
                      "差分取得を終了します。")
                break
            prev_until = until
            until = min(item["id"] for item in items)
            if until == prev_until:
                break
            time.sleep(PAGE_SLEEP)
        common.set_state(con, f"misskey:{host}:{uid}:reactions_done", "1")
    except MkError as e:
        if r_diff:
            con.execute("DELETE FROM sync_state WHERE key=?",
                        (f"misskey:{host}:{uid}:reactions_done",))
        print(f"  ! リアクション履歴を取得できませんでした（{e}）")
        print("    → サーバの「設定 → プライバシー → リアクション履歴を公開する」を"
              "有効にすると取得できる場合があります。")
    con.commit()
    con.close()
    print(f"---- 完了: 投稿 {n['post']} / リノート {n['repost']} / いいね {n_like} ----")


# ---------------- エクスポートJSONモード ----------------

def find_export_json(path: Path):
    """notes エクスポート(.json / .zip / フォルダ)を解決。(jsonパス, 展開ルート or None)。"""
    extracted_root = None
    if path.is_file() and path.suffix.lower() == ".json":
        return path, None
    if path.is_file() and path.suffix.lower() == ".zip":
        out = (common.ROOT / "misskey_archive_extracted"
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
        js = sorted(path.rglob("*.json"), key=lambda f: -f.stat().st_size)
        if js:
            return js[0], extracted_root
    raise MkError(0, f"エクスポートJSONが見つかりません: {path}")


def from_export(path, host, user, cfg, skip_media=False, keep_extracted=False,
                account_override=None, token=None):
    tz = common.get_tz(cfg)
    account = account_override or f"@{user}@{host}"
    jf, extracted_root = find_export_json(Path(path).expanduser())
    print(f"=== Misskeyエクスポート取り込み: {account} ← {jf.name} ===")
    data = json.loads(jf.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise MkError(0, "エクスポートJSONの形式が想定と異なります（配列であるはず）。")
    sess = requests.Session()
    dl = None if skip_media else (lambda url, dest: common.download(url, dest, sess))
    con = common.connect()
    n = n_stub = 0
    for note in data:
        if note.get("renoteId") and note.get("text") is None and not note.get("renote"):
            # エクスポートにはリノート先の本文が含まれない → スタブ（あとで補完）
            dt = common.parse_iso(note.get("createdAt"))
            c_utc, c_loc = common.iso_pair(dt, tz)
            common.upsert_post(
                con, service="misskey", account=account, post_id=note["id"],
                type="repost", created_at=c_utc, created_local=c_loc,
                text="（リノート — 本文は未取得です）",
                author_handle=None, author_name=None,
                url=f"https://{host}/notes/{note['id']}",
                is_reply=0,
                extra={"export_stub": True,
                       "renote_of": f"https://{host}/notes/{note.get('renoteId')}"})
            n_stub += 1
        else:
            ingest_note(con, tz, host, account, note, dl)
            if ((note.get("fileIds") and not note.get("files"))
                    or (note.get("renoteId") and not note.get("renote"))):
                # 添付や引用先の情報が本体に含まれていない → 補完対象
                mark_stub(con, account, note["id"])
                n_stub += 1
        n += 1
        if n % 1000 == 0:
            con.commit()
            print(f"  … {n} 件処理")
    con.commit()
    print(f"---- 取り込み完了: {n} 件（うち補完待ち {n_stub} 件）----")
    enrich_export_stubs(con, cfg, host, account, sess, skip_media, token=token)
    con.close()
    common.cleanup_extracted(extracted_root, keep=keep_extracted)


def mark_stub(con, account, note_id):
    row = con.execute(
        "SELECT id, extra FROM posts WHERE service='misskey' AND account=? "
        "AND post_id=? AND type IN ('post','repost')", (account, note_id)).fetchone()
    if not row:
        return
    extra = json.loads(row["extra"]) if row["extra"] else {}
    extra["export_stub"] = True
    con.execute("UPDATE posts SET extra=? WHERE id=?",
                (json.dumps(extra, ensure_ascii=False), row["id"]))


def enrich_export_stubs(con, cfg, host, account, sess, skip_media, token=None):
    """補完待ちノートをAPI(notes/show)で取得し直して埋める。
    トークンは無くてもよい（公開ノートのみ補完）。あれば非公開のリノート元も取得できる。"""
    tz = common.get_tz(cfg)
    rows = con.execute(
        "SELECT id, post_id FROM posts WHERE service='misskey' AND account=? "
        "AND extra LIKE '%\"export_stub\"%'", (account,)).fetchall()
    if not rows:
        return
    print(f"公開APIで {len(rows)} 件の本文・メディアを補完しています…")
    dl = None if skip_media else (lambda url, dest: common.download(url, dest, sess))
    ok = gone = fail = 0
    net_errors = 0
    for i, row in enumerate(rows):
        try:
            note = api(sess, host, "notes/show", {"noteId": row["post_id"]}, token)
        except MkError as e:
            if e.status in (400, 404):     # NO_SUCH_NOTE = 削除済み
                cur = con.execute("SELECT text, extra FROM posts WHERE id=?",
                                  (row["id"],)).fetchone()
                extra = json.loads(cur["extra"]) if cur["extra"] else {}
                extra.pop("export_stub", None)
                extra["gone"] = True
                text = cur["text"]
                if text.startswith("（リノート — 本文は未取得"):
                    text = "（リノート元は削除されています）"
                con.execute("UPDATE posts SET text=?, extra=? WHERE id=?",
                            (text, json.dumps(extra, ensure_ascii=False), row["id"]))
                gone += 1
            else:
                fail += 1
                net_errors += 1
                if net_errors >= 5:
                    print("  ! サーバに接続できないため補完を中断します"
                          "（サーバ稼働中に sync_misskey.bat を再実行すると続きから補完されます）")
                    break
            continue
        net_errors = 0
        if note:
            ingest_note(con, tz, host, account, note, dl)  # upsertでスタブが上書きされる
            ok += 1
        time.sleep(0.3)
        if (i + 1) % 50 == 0:
            con.commit()
            print(f"  … {i + 1}/{len(rows)} 件補完")
    con.commit()
    print(f"---- 補完完了: 成功 {ok} / 削除済み {gone} / 保留 {fail} ----")
    if ok or gone:
        print("※ リアクション（いいね）履歴はエクスポートに含まれないため、"
              "取得にはAPIモード（token設定）が必要です。")


def main():
    ap = argparse.ArgumentParser(description="Misskey取り込み")
    ap.add_argument("--skip-media", action="store_true", help="メディアをダウンロードしない")
    ap.add_argument("--skip-like-media", action="store_true",
                    help="いいねした投稿のメディアはダウンロードしない")
    ap.add_argument("--full", action="store_true", help="差分ではなく全件を取得し直す")
    ap.add_argument("--from-export", metavar="JSON", help="手動エクスポートJSONから取り込む")
    ap.add_argument("--host", help="--from-export 時のサーバホスト名")
    ap.add_argument("--user", help="--from-export 時の自分のユーザー名")
    ap.add_argument("--keep-extracted", action="store_true",
                    help="zipから展開した一時データを取り込み後に削除しない")
    args = ap.parse_args()

    cfg = common.load_config()
    keep = args.keep_extracted or bool(cfg.get("keep_extracted"))

    if args.from_export:
        if not (args.host and args.user):
            sys.exit("--from-export には --host と --user の指定が必要です。")
        from_export(args.from_export, args.host, args.user, cfg,
                    skip_media=args.skip_media, keep_extracted=keep)
        return

    entries = []
    for s in common.as_list(cfg.get("misskey")):
        if (s.get("host") or "").strip() and (s.get("token") or "").strip():
            entries.append(("api", s))
        elif (s.get("archive") or "").strip():
            entries.append(("export", s))
    if not entries:
        print("misskey の設定が無いためスキップします（config.json の misskey）")
        return
    tz = common.get_tz(cfg)
    like_media = cfg.get("download_like_media", True)
    if args.full:
        con = common.connect()
        con.execute("DELETE FROM sync_state WHERE key LIKE 'misskey:%'")
        con.commit()
        con.close()
    for i, (mode, server) in enumerate(entries):
        if len(entries) > 1:
            print(f"\n##### Misskeyアカウント {i + 1}/{len(entries)} #####")
        try:
            if mode == "api":
                sync_server(cfg, server, tz, args.skip_media,
                            args.skip_like_media or not like_media)
            else:
                host = (server.get("host") or "").strip()
                user = (server.get("user") or "").strip().lstrip("@")
                if not host or not user:
                    print("! archive形式のmisskeyには host と user の指定が必要です"
                          "（例: \"host\": \"misskey.example.jp\", \"user\": \"あなたのID\"）")
                    continue
                from_export(server["archive"], host, user, cfg,
                            skip_media=args.skip_media, keep_extracted=keep)
        except MkError as e:
            print(f"! {server.get('host') or server.get('archive')} でエラー: {e}")
            if mode == "api":
                print("  トークンの権限・ホスト名を確認してください。")


if __name__ == "__main__":
    main()
