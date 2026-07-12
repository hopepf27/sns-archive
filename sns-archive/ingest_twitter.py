# -*- coding: utf-8 -*-
"""
ingest_twitter.py — Twitter公式アーカイブ（zip / 展開済みフォルダ）を取り込む

取り込むもの:
  - ツイート（tweets.js / tweet.js、分割ファイル対応）
  - リツイート（本文が "RT @user: ..." の形式のもの）
  - いいね（like.js）… アーカイブに「いいねした日時」は含まれないため、
    元ツイートIDから投稿日時を復元（Snowflake ID）して時系列に配置します
  - メディア（data/tweets_media/ 内のファイルを media/twitter/ にコピー）
"""
import argparse
import html
import json
import re
import shutil
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import common

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

TW_DATE = re.compile(
    r"^\w{3} (\w{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) ([+-]\d{4}) (\d{4})$")

RT_RE = re.compile(r"^RT @([0-9A-Za-z_]{1,20}):\s?(.*)$", re.S)


def parse_tw_date(s):
    """'Wed Oct 10 20:19:24 +0000 2018' 形式をロケール非依存でパース。"""
    m = TW_DATE.match((s or "").strip())
    if not m:
        return None
    mon, day, hh, mm, ss, off, year = m.groups()
    if mon not in MONTHS:
        return None
    sign = 1 if off[0] == "+" else -1
    tz = timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
    return datetime(int(year), MONTHS[mon], int(day),
                    int(hh), int(mm), int(ss), tzinfo=tz)


def snowflake_dt(tweet_id):
    """ツイートID(Snowflake)から投稿日時を復元。復元不能なら None。"""
    try:
        i = int(str(tweet_id))
    except (TypeError, ValueError):
        return None
    if i < (1 << 22):
        return None
    ms = (i >> 22) + 1288834974657
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    lo = datetime(2010, 11, 1, tzinfo=timezone.utc)
    hi = datetime.now(timezone.utc) + timedelta(days=2)
    return dt if lo <= dt <= hi else None


def read_ytd(path: Path):
    """window.YTD.xxx.part0 = [...] 形式のJSファイルを読む。"""
    text = path.read_text(encoding="utf-8")
    idx = text.find("=")
    if idx < 0:
        return []
    return json.loads(text[idx + 1:])


def resolve_archive_dir(p: Path):
    """zipなら展開し、(data_dir, 展開ルート or None) を返す。
    展開ルートは zip から展開した一時フォルダ（取り込み後に削除できる）。
    ユーザーが元々フォルダを指定した場合は None（削除しない）。"""
    extracted_root = None
    if p.is_file() and p.suffix.lower() == ".zip":
        out = (common.ROOT / "twitter_archive_extracted"
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
    for cand in (p, p / "data"):
        if cand.is_dir() and (list(cand.glob("tweets*.js")) or (cand / "tweet.js").exists()
                              or list(cand.glob("tweet-part*.js"))):
            return cand, extracted_root
    sys.exit(f"tweets.js が見つかりません: {p}\n"
             "config.json の twitter.archive_dir に、Twitterアーカイブの"
             "zipファイルまたは展開済みフォルダのパスを指定してください。")


def collect_js(data_dir: Path, base_names):
    """tweets.js / tweets-part1.js ... のような分割ファイルを集める。"""
    pat = re.compile(rf"^({'|'.join(base_names)})(-part\d+)?\.js$")
    files = sorted(f for f in data_dir.iterdir()
                   if f.is_file() and pat.match(f.name))
    return files


def account_username(data_dir: Path):
    f = data_dir / "account.js"
    if not f.exists():
        return None
    try:
        items = read_ytd(f)
        return items[0].get("account", {}).get("username")
    except Exception:
        return None


def media_entity_map(t):
    """{アーカイブ内ファイル名の末尾部分: (remote_url, entity_type)} を作る。"""
    ee = t.get("extended_entities") or {}
    ents = ee.get("media") or (t.get("entities") or {}).get("media") or []
    out = {}
    for m in ents:
        mtype = m.get("type", "photo")
        u = m.get("media_url_https") or m.get("media_url") or ""
        if u:
            out[u.rsplit("/", 1)[-1]] = (u, mtype)
        best = None
        for v in (m.get("video_info") or {}).get("variants") or []:
            if v.get("content_type") == "video/mp4" and v.get("url"):
                br = int(v.get("bitrate") or 0)
                if best is None or br > best[0]:
                    best = (br, v["url"])
        if best:
            name = best[1].split("?")[0].rsplit("/", 1)[-1]
            out[name] = (best[1], mtype)
    return out


def kind_from_ext(path: Path):
    e = path.suffix.lower()
    if e in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heic"):
        return "image"
    if e in (".mp4", ".mov", ".webm", ".m4v"):
        return "video"
    if e in (".mp3", ".m4a", ".wav", ".ogg", ".aac"):
        return "audio"
    return "file"


def attach_media(con, pid, tid, media_src_dirs, entmap):
    items = []
    dest_dir = common.MEDIA_DIR / "twitter"
    for src_dir in media_src_dirs:
        for f in sorted(src_dir.glob(f"{tid}-*")):
            if not f.is_file():
                continue
            dest = dest_dir / f.name
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                if (not dest.exists()) or dest.stat().st_size != f.stat().st_size:
                    shutil.copy2(f, dest)
            except Exception as e:
                print(f"    ! メディアコピー失敗 {f.name}: {e}")
                continue
            base = f.name[len(str(tid)) + 1:]
            remote, etype = entmap.get(base, ("", None))
            kind = kind_from_ext(f)
            if etype == "animated_gif" and kind == "video":
                kind = "gifv"
            items.append({"kind": kind,
                          "local_path": f"twitter/{f.name}",
                          "remote_url": remote or None,
                          "alt": None})
    if items:
        common.replace_media(con, pid, items)


def clean_text(t, tweet):
    """HTMLエンティティ解除・t.co展開・メディアURL除去。"""
    text = html.unescape(t or "")
    ents = tweet.get("entities") or {}
    for u in ents.get("urls") or []:
        if u.get("url") and u.get("expanded_url"):
            text = text.replace(u["url"], u["expanded_url"])
    media_ents = ((tweet.get("extended_entities") or {}).get("media")
                  or ents.get("media") or [])
    for m in media_ents:
        if m.get("url"):
            text = text.replace(m["url"], "")
    return text.strip()


def main():
    ap = argparse.ArgumentParser(description="Twitterアーカイブ取り込み")
    ap.add_argument("--archive", help="アーカイブzipまたはフォルダ（config.jsonより優先）")
    ap.add_argument("--full", action="store_true",
                    help="取り込み済みのアーカイブでも強制的に読み込み直す")
    ap.add_argument("--keep-extracted", action="store_true",
                    help="zipから展開した一時データを取り込み後に削除しない")
    args = ap.parse_args()

    cfg = common.load_config()
    keep = args.keep_extracted or bool(cfg.get("keep_extracted"))
    if args.archive:
        entries = [{"archive_dir": args.archive}]
    else:
        entries = common.as_list(cfg.get("twitter"))
        entries = [e for e in entries if (e.get("archive_dir") or "").strip()]
    if not entries:
        print("twitter の設定が無いためスキップします（config.json の twitter）")
        return
    for i, tw in enumerate(entries):
        if len(entries) > 1:
            print(f"\n##### Twitterアカウント {i + 1}/{len(entries)} #####")
        try:
            ingest_one(cfg, tw, keep_extracted=keep, force=args.full)
        except SystemExit as e:
            print(f"! スキップ: {e}")


def _archive_signature(path: Path):
    """アーカイブの同一性判定に使う署名（サイズと更新時刻）。
    zipはファイル自体、フォルダは中の tweets.js を代表として見る。"""
    try:
        target = path
        if path.is_dir():
            for name in ("data/tweets.js", "tweets.js", "data/tweet.js", "tweet.js"):
                cand = path / name
                if cand.is_file():
                    target = cand
                    break
            else:
                return None          # 代表ファイル不明 → 毎回取り込む（安全側）
        st = target.stat()
        return f"{st.st_size},{st.st_mtime_ns}"
    except OSError:
        return None


def ingest_one(cfg, tw, keep_extracted=False, force=False):
    src = tw.get("archive_dir")
    src_path = Path(src).expanduser()

    # 同じアーカイブは2回目以降スキップする（sync_all を毎日回しても安全）。
    # ファイルを差し替えたり更新すれば自動で再取り込みされる。
    con0 = common.connect()
    seen_key = f"twitter:archive_seen:{src_path.resolve()}"
    sig = _archive_signature(src_path)
    if not force and sig and common.get_state(con0, seen_key) == sig:
        print(f"=== Twitter取り込み: {src_path.name} ===")
        print("  このアーカイブは取り込み済みのためスキップしました。")
        print("  （新しいアーカイブに差し替えれば自動で取り込みます。"
              "強制的に読み直す場合は --full）")
        con0.close()
        return
    con0.close()

    data_dir, extracted_root = resolve_archive_dir(src_path)
    tz = common.get_tz(cfg)

    username = account_username(data_dir)
    if (tw.get("account_label") or "").strip():
        account = tw["account_label"].strip()
    elif username:
        account = f"@{username}"
    else:
        # username が取れないときはフォルダ名で区別（複数アカウントの取り違え防止）
        tag = common.sanitize(data_dir.parent.name or data_dir.name, 30)
        account = f"@twitter_{tag}" if tag and tag != "data" else "@twitter"
    print(f"=== Twitter取り込み: {account} ===")
    print(f"データフォルダ: {data_dir}")

    media_dirs = [d for d in (data_dir / "tweets_media", data_dir / "tweet_media") if d.is_dir()]
    if not media_dirs:
        print("  (tweets_media フォルダが見つかりません。メディアなしで続行)")

    con = common.connect()
    n_post = n_rt = n_reply = 0

    tweet_files = collect_js(data_dir, ["tweets", "tweet"])
    tweet_files = [f for f in tweet_files if not f.name.startswith("tweet-headers")]
    if not tweet_files:
        sys.exit("ツイートデータ(tweets.js)が見つかりませんでした。")

    for jf in tweet_files:
        print(f"読み込み中: {jf.name}")
        try:
            items = read_ytd(jf)
        except Exception as e:
            print(f"  ! 読み込み失敗: {e}")
            continue
        for i, item in enumerate(items):
            t = item.get("tweet", item)
            tid = t.get("id_str") or str(t.get("id") or "")
            if not tid:
                continue
            raw = t.get("full_text") or t.get("text") or ""
            text = clean_text(raw, t)
            dt = parse_tw_date(t.get("created_at")) or snowflake_dt(tid)
            c_utc, c_loc = common.iso_pair(dt, tz)
            url = (f"https://x.com/{username}/status/{tid}" if username
                   else f"https://x.com/i/web/status/{tid}")
            extra = {}
            ptype = "post"
            author_handle = account
            author_name = None

            m = RT_RE.match(text)
            if m:
                ptype = "repost"
                author_handle = "@" + m.group(1)
                text = m.group(2)
                extra["rt_truncated"] = True  # アーカイブ仕様で140字前後に切り詰め
                n_rt += 1
            else:
                n_post += 1

            is_reply = 0
            r_to = t.get("in_reply_to_screen_name")
            if r_to or t.get("in_reply_to_status_id_str"):
                is_reply = 1
                n_reply += 1
                if r_to:
                    extra["reply_to"] = "@" + r_to

            fav = t.get("favorite_count")
            rtc = t.get("retweet_count")
            if fav not in (None, "0", 0):
                extra["fav_count"] = int(fav)
            if rtc not in (None, "0", 0):
                extra["rt_count"] = int(rtc)

            pid = common.upsert_post(
                con, service="twitter", account=account, post_id=tid,
                type=ptype, created_at=c_utc, created_local=c_loc,
                text=text, author_handle=author_handle, author_name=author_name,
                url=url, is_reply=is_reply, extra=extra or None)
            entmap = media_entity_map(t)
            attach_media(con, pid, tid, media_dirs, entmap)

            if (i + 1) % 2000 == 0:
                con.commit()
                print(f"  … {i + 1} 件処理")
        con.commit()

    # ---- いいね ----
    n_like = n_like_dated = 0
    like_files = collect_js(data_dir, ["like", "likes"])
    for jf in like_files:
        print(f"読み込み中: {jf.name}")
        try:
            items = read_ytd(jf)
        except Exception as e:
            print(f"  ! 読み込み失敗: {e}")
            continue
        for i, item in enumerate(items):
            lk = item.get("like", item)
            tid = lk.get("tweetId") or lk.get("tweet_id")
            if not tid:
                continue
            text = html.unescape(lk.get("fullText") or lk.get("full_text") or "")
            dt = snowflake_dt(tid)
            c_utc, c_loc = common.iso_pair(dt, tz)
            extra = {}
            if dt:
                extra["date_estimated"] = True  # いいねした日時ではなく元ツイートの投稿日時
                n_like_dated += 1
            url = lk.get("expandedUrl") or f"https://x.com/i/web/status/{tid}"
            common.upsert_post(
                con, service="twitter", account=account, post_id=str(tid),
                type="like", created_at=c_utc, created_local=c_loc,
                text=text or "（本文はアーカイブに含まれていません）",
                author_handle=None, author_name=None,
                url=url, is_reply=0, extra=extra or None)
            n_like += 1
            if (i + 1) % 5000 == 0:
                con.commit()
        con.commit()

    con.commit()
    # 取り込みが最後まで成功したので、このアーカイブを取り込み済みとして記録
    if sig:
        common.set_state(con, seen_key, sig)
        con.commit()
    con.close()
    print("---- 完了 ----")
    print(f"投稿 {n_post} / RT {n_rt} / うち返信 {n_reply} / いいね {n_like}"
          f"（日時復元済み {n_like_dated}）")
    if n_like:
        print("※ いいねの日時は元ツイートの投稿日時から復元した推定値です"
              "（Twitterアーカイブに「いいねした日時」は含まれないため）。")

    # zipから展開した一時データは、取り込み完了後に削除（メディアは media/ にコピー済み）。
    common.cleanup_extracted(extracted_root, keep=keep_extracted)


if __name__ == "__main__":
    main()
