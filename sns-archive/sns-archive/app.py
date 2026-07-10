# -*- coding: utf-8 -*-
"""
app.py — 統合SNSアーカイブ ビューア（ローカルWebサーバ）

起動:  python app.py            → http://127.0.0.1:5089/ をブラウザで開く
       python app.py --no-browser --port 5089
"""
import argparse
import calendar
import json
import re
import threading
import webbrowser

from flask import Flask, jsonify, render_template, request, send_from_directory

import common

app = Flask(__name__)

# ---------------------------------------------------------------
# 検索クエリパーサ
# ---------------------------------------------------------------

TOKEN_RE = re.compile(r'(-?)"([^"]+)"|(\S+)')

KEY_ALIASES = {
    "service": "service", "svc": "service", "on": "service", "sns": "service",
    "account": "account", "acct": "account",
    "from": "from", "by": "from", "user": "from",
    "type": "type", "is": "type",
    "has": "has", "filter": "has",
    "since": "since", "after": "since",
    "until": "until", "before": "until",
    "date": "date", "day": "date",
    "sort": "sort", "order": "sort",
    "label": "label", "bm": "label", "bookmark": "label",
}

SERVICE_ALIASES = {
    "twitter": "twitter", "x": "twitter", "tw": "twitter",
    "misskey": "misskey", "mi": "misskey", "mk": "misskey",
    "bluesky": "bluesky", "bsky": "bluesky", "bs": "bluesky",
    "mastodon": "mastodon", "mstdn": "mastodon", "don": "mastodon",
    "md": "mastodon", "fedi": "mastodon",
}

TYPE_ALIASES = {
    "post": "post", "posts": "post", "note": "post", "tweet": "post",
    "toot": "post",
    "repost": "repost", "rt": "repost", "renote": "repost", "rn": "repost",
    "boost": "repost",
    "like": "like", "likes": "like", "fav": "like", "favorite": "like",
    "favourite": "like", "reaction": "like", "star": "like",
}


def esc_like(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def norm_date(v, end=False):
    """YYYY / YYYY-MM / YYYY-MM-DD → created_local 比較用の文字列。"""
    v = v.replace("/", "-").replace(".", "-")
    m = re.match(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$", v)
    if not m:
        return None
    y = int(m.group(1))
    mo = int(m.group(2)) if m.group(2) else None
    d = int(m.group(3)) if m.group(3) else None
    try:
        if not end:
            return f"{y:04d}-{mo or 1:02d}-{d or 1:02d}T00:00:00"
        if d:
            return f"{y:04d}-{mo:02d}-{d:02d}T23:59:59"
        if mo:
            last = calendar.monthrange(y, mo)[1]
            return f"{y:04d}-{mo:02d}-{last:02d}T23:59:59"
        return f"{y:04d}-12-31T23:59:59"
    except Exception:
        return None


def build_query(q, ui):
    """q: 検索文字列, ui: dict(services, accounts, types, replies, month)
    → (where_sql_list, params, order_sql)"""
    where, params = [], []
    text_groups, text_not = [], []
    svc_in, svc_not = [], []
    acct_in, acct_not = [], []
    type_in, type_not = [], []
    from_in, from_not = [], []
    has_in, has_not = [], []
    label_in, label_not = [], []
    reply_req = None
    since = until = date_md = None
    sort = None
    pending_or = False

    def add_text(term, neg):
        nonlocal pending_or
        if not term:
            return
        if neg:
            text_not.append(term)
        elif pending_or and text_groups:
            text_groups[-1].append(term)
        else:
            text_groups.append([term])
        pending_or = False

    for m in TOKEN_RE.finditer(q or ""):
        if m.group(2) is not None:
            add_text(m.group(2), m.group(1) == "-")
            continue
        tok = m.group(3)
        if tok in ("OR", "|"):
            pending_or = True
            continue
        neg = tok.startswith("-") and len(tok) > 1
        if neg:
            tok = tok[1:]
        key = val = None
        if ":" in tok:
            k, v = tok.split(":", 1)
            if k.lower() in KEY_ALIASES and v:
                key, val = KEY_ALIASES[k.lower()], v
        if not key:
            add_text(tok, neg)
            continue
        pending_or = False
        if key == "service":
            s = SERVICE_ALIASES.get(val.lower(), val.lower())
            (svc_not if neg else svc_in).append(s)
        elif key == "account":
            (acct_not if neg else acct_in).append(val)
        elif key == "from":
            (from_not if neg else from_in).append(val.lstrip("@"))
        elif key == "type":
            v = val.lower()
            if v in ("reply", "replies"):
                reply_req = (False if neg else True)
            elif v in ("media", "image", "video", "photo", "pic", "link"):
                (has_not if neg else has_in).append(
                    "image" if v in ("photo", "pic") else v)
            elif v in TYPE_ALIASES:
                (type_not if neg else type_in).append(TYPE_ALIASES[v])
        elif key == "has":
            v = val.lower()
            v = "image" if v in ("photo", "pic", "images") else v
            v = "media" if v in ("medias",) else v
            v = "video" if v in ("videos", "movie") else v
            v = "bookmark" if v in ("bookmarks", "bm", "label", "labels") else v
            if v in ("media", "image", "video", "link", "bookmark"):
                (has_not if neg else has_in).append(v)
        elif key == "since":
            since = norm_date(val) or since
        elif key == "until":
            until = norm_date(val, end=True) or until
        elif key == "date":
            v = val.replace("/", "-").replace(".", "-")
            if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", v):
                since = norm_date(v) or since
                until = norm_date(v, end=True) or until
            else:
                m2 = re.match(r"^(\d{1,2})-(\d{1,2})$", v)
                if m2:
                    date_md = f"{int(m2.group(1)):02d}-{int(m2.group(2)):02d}"
        elif key == "label":
            (label_not if neg else label_in).append(val)
        elif key == "sort":
            v = val.lower()
            if v in ("asc", "old", "oldest"):
                sort = "asc"
            elif v in ("desc", "new", "newest"):
                sort = "desc"
            elif v in ("random", "rand", "shuffle"):
                sort = "random"

    # ---- テキスト条件（本文＋メディアのalt説明文を対象） ----
    def term_sql(term):
        pat = f"%{esc_like(term)}%"
        if common.fts_enabled() and len(term) >= 3:
            # trigramインデックスで部分一致（3文字以上の語のみ有効）。
            # 画像の説明文(alt)はUNIONで同じIN句に含め、インデックスを活かす。
            params.extend(['"' + term.replace('"', '""') + '"', pat])
            return ("(posts.id IN (SELECT rowid FROM posts_fts "
                    "WHERE posts_fts MATCH ? "
                    "UNION SELECT ma.post_id FROM media ma "
                    "WHERE ma.alt LIKE ? ESCAPE '\\'))")
        params.extend([pat, pat])
        return ("(posts.text LIKE ? ESCAPE '\\' OR EXISTS("
                "SELECT 1 FROM media ma WHERE ma.post_id=posts.id "
                "AND ma.alt LIKE ? ESCAPE '\\'))")

    for group in text_groups:
        where.append("(" + " OR ".join(term_sql(t) for t in group) + ")")
    for term in text_not:
        where.append("NOT " + term_sql(term))

    # ---- フィルタ条件 ----
    def in_clause(col, values, negate=False):
        ph = ",".join("?" * len(values))
        where.append(f"{col} {'NOT ' if negate else ''}IN ({ph})")
        params.extend(values)

    if svc_in:
        in_clause("posts.service", svc_in)
    if svc_not:
        in_clause("posts.service", svc_not, negate=True)
    if type_in:
        in_clause("posts.type", type_in)
    if type_not:
        in_clause("posts.type", type_not, negate=True)

    def like_any(cols_terms, values, negate=False):
        blocks = []
        for v in values:
            ors = []
            for col in cols_terms:
                ors.append(f"{col} LIKE ? ESCAPE '\\'")
                params.append(f"%{esc_like(v)}%")
            blocks.append("(" + " OR ".join(ors) + ")")
        clause = "(" + " OR ".join(blocks) + ")"
        where.append(f"NOT {clause}" if negate else clause)

    if acct_in:
        like_any(["posts.account"], acct_in)
    if acct_not:
        like_any(["posts.account"], acct_not, negate=True)
    if from_in:
        like_any(["posts.author_handle", "posts.author_name"], from_in)
    if from_not:
        like_any(["posts.author_handle", "posts.author_name"], from_not, negate=True)

    HAS_SQL = {
        "media": "EXISTS(SELECT 1 FROM media m WHERE m.post_id=posts.id)",
        "image": "EXISTS(SELECT 1 FROM media m WHERE m.post_id=posts.id "
                 "AND m.kind IN ('image','gifv'))",
        "video": "EXISTS(SELECT 1 FROM media m WHERE m.post_id=posts.id "
                 "AND m.kind IN ('video','gifv','remote_video'))",
        "link": "posts.text LIKE '%http%'",
        "bookmark": "EXISTS(SELECT 1 FROM bookmarks b WHERE b.post_id=posts.id)",
    }
    for h in has_in:
        where.append(HAS_SQL[h])
    for h in has_not:
        where.append("NOT " + HAS_SQL[h])

    def label_sql(v):
        if v in ("*", "any", "all"):
            return "EXISTS(SELECT 1 FROM bookmarks b WHERE b.post_id=posts.id)"
        params.append(v)
        return ("EXISTS(SELECT 1 FROM bookmarks b "
                "WHERE b.post_id=posts.id AND b.label=?)")

    for v in label_in:
        where.append(label_sql(v))
    for v in label_not:
        where.append("NOT " + label_sql(v))

    if reply_req is True:
        where.append("posts.is_reply=1")
    elif reply_req is False:
        where.append("posts.is_reply=0")
    if since:
        where.append("posts.created_local >= ?")
        params.append(since)
    if until:
        where.append("posts.created_local <= ?")
        params.append(until)
    if date_md:
        where.append("substr(posts.created_local, 6, 5) = ?")
        params.append(date_md)

    # ---- UI側フィルタ（検索クエリとAND） ----
    if ui.get("services"):
        in_clause("posts.service", ui["services"])
    if ui.get("accounts"):
        ph = ",".join("?" * len(ui["accounts"]))
        where.append(f"(posts.service || '|' || posts.account) IN ({ph})")
        params.extend(ui["accounts"])
    if ui.get("types"):
        in_clause("posts.type", ui["types"])
    if ui.get("replies") == "0":
        where.append("posts.is_reply=0")
    elif ui.get("replies") == "only":
        where.append("posts.is_reply=1")
    if ui.get("label"):
        where.append(label_sql(ui["label"]))
    month = ui.get("month")
    if month == "none":
        where.append("posts.created_local IS NULL")
    elif month and re.match(r"^\d{4}-\d{2}$", month):
        where.append("posts.ym = ?")   # 生成列＋インデックスで高速
        params.append(month)

    order = ui.get("order") or sort or "desc"
    if order == "random":
        order_sql = "ORDER BY RANDOM()"
    elif order == "asc":
        order_sql = ("ORDER BY posts.created_local IS NULL, "
                     "posts.created_local ASC, posts.id ASC")
    else:
        order = "desc"
        order_sql = "ORDER BY posts.created_local DESC, posts.id DESC"
    return where, params, order_sql, order


def attach_details(con, posts):
    """投稿行のリストに media / labels / extra を付与する（run_search と文脈表示で共用）。"""
    ids = [p["id"] for p in posts]
    media_map = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for m in con.execute(
                f"SELECT * FROM media WHERE post_id IN ({ph}) "
                "ORDER BY post_id, sort", ids):
            media_map.setdefault(m["post_id"], []).append({
                "kind": m["kind"],
                "url": ("/media/" + m["local_path"]) if m["local_path"] else None,
                "remote_url": m["remote_url"],
                "alt": m["alt"],
            })
    label_map = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for b in con.execute(
                f"SELECT post_id, label FROM bookmarks WHERE post_id IN ({ph}) "
                "ORDER BY label", ids):
            label_map.setdefault(b["post_id"], []).append(b["label"])
    for p in posts:
        p["media"] = media_map.get(p["id"], [])
        p["labels"] = label_map.get(p["id"], [])
        p["extra"] = json.loads(p["extra"]) if p.get("extra") else {}
    return posts


def run_search(q, ui, page, per):
    con = common.connect()
    try:
        where, params, order_sql, order = build_query(q, ui)
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        # 件数と集計は1ページ目だけ計算する（スクロール継続時の負荷を削減）
        total = (con.execute(f"SELECT COUNT(*) FROM posts {wsql}", params).fetchone()[0]
                 if page == 1 else -1)
        offset = (page - 1) * per
        rows = con.execute(
            f"SELECT * FROM posts {wsql} {order_sql} LIMIT ? OFFSET ?",
            [*params, per, offset]).fetchall()
        posts = attach_details(con, [dict(r) for r in rows])
        result = {"total": total, "page": page, "per": per,
                  "order": order, "posts": posts}
        if page == 1:
            # 月別ヒストグラムとサービス別件数を1回のGROUP BYでまとめて取得。
            # サービス別合計は Python 側で集約する（クエリを2→1に削減）。
            hist = con.execute(
                f"SELECT COALESCE(posts.ym,'none') ym, "
                f"posts.service, COUNT(*) c FROM posts {wsql} "
                "GROUP BY ym, posts.service", params).fetchall()
            result["histogram"] = [
                {"ym": r["ym"], "service": r["service"], "c": r["c"]} for r in hist]
            svc_counts = {}
            for r in hist:
                svc_counts[r["service"]] = svc_counts.get(r["service"], 0) + r["c"]
            result["service_counts"] = svc_counts
            # 検索欄直下の内訳表示用: アカウント別件数（サービス色を添えるため service も返す）
            acc = con.execute(
                f"SELECT posts.account, posts.service, COUNT(*) c "
                f"FROM posts {wsql} "
                "GROUP BY posts.account, posts.service ORDER BY c DESC, posts.account",
                params).fetchall()
            result["account_counts"] = [
                {"account": r["account"], "service": r["service"], "c": r["c"]}
                for r in acc]
        return result
    finally:
        con.close()


# ---------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/meta")
def meta():
    con = common.connect()
    try:
        accounts = [dict(r) for r in con.execute(
            "SELECT service, account, COUNT(*) c, "
            "SUM(type='post') posts, SUM(type='repost') reposts, "
            "SUM(type='like') likes "
            "FROM posts GROUP BY service, account "
            "ORDER BY service, account")]
        totals = dict(con.execute(
            "SELECT type, COUNT(*) FROM posts GROUP BY type").fetchall())
        rng = con.execute(
            "SELECT MIN(created_local), MAX(created_local) FROM posts "
            "WHERE created_local IS NOT NULL").fetchone()
        return jsonify({
            "accounts": accounts,
            "totals": {"post": totals.get("post", 0),
                       "repost": totals.get("repost", 0),
                       "like": totals.get("like", 0),
                       "all": sum(totals.values())},
            "range": {"min": rng[0], "max": rng[1]},
        })
    finally:
        con.close()


@app.route("/api/posts")
def api_posts():
    q = request.args.get("q", "")
    page = max(1, request.args.get("page", 1, type=int))
    per = min(200, max(1, request.args.get("per", 50, type=int)))
    ui = {
        "services": [s for s in (request.args.get("services") or "").split(",") if s],
        "accounts": [a for a in (request.args.get("accounts") or "").split("\n") if a],
        "types": [t for t in (request.args.get("types") or "").split(",") if t],
        "replies": request.args.get("replies", "1"),
        "month": request.args.get("month") or None,
        "order": request.args.get("order") or None,
        "label": (request.args.get("label") or "").strip() or None,
    }
    return jsonify(run_search(q, ui, page, per))


@app.route("/api/labels")
def api_labels():
    con = common.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT label, COUNT(*) c FROM bookmarks "
            "GROUP BY label ORDER BY c DESC, label")]
        total = con.execute(
            "SELECT COUNT(DISTINCT post_id) FROM bookmarks").fetchone()[0]
        return jsonify({"labels": rows, "total_posts": total})
    finally:
        con.close()


@app.route("/api/bookmark", methods=["POST"])
def api_bookmark():
    data = request.get_json(force=True, silent=True) or {}
    try:
        post_id = int(data.get("post_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "post_id が不正です"}), 400
    label = str(data.get("label") or "").strip()[:50]
    on = bool(data.get("on"))
    if not label:
        return jsonify({"error": "ラベル名が空です"}), 400
    con = common.connect()
    try:
        if not con.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
            return jsonify({"error": "投稿が見つかりません"}), 404
        if on:
            con.execute("INSERT OR IGNORE INTO bookmarks(post_id, label) VALUES(?,?)",
                        (post_id, label))
        else:
            con.execute("DELETE FROM bookmarks WHERE post_id=? AND label=?",
                        (post_id, label))
        con.commit()
        labels = [r[0] for r in con.execute(
            "SELECT label FROM bookmarks WHERE post_id=? ORDER BY label", (post_id,))]
        return jsonify({"ok": True, "labels": labels})
    finally:
        con.close()


@app.route("/api/context")
def api_context():
    """指定した投稿の前後（同じアカウントの時系列）を返す。

    id=<posts.id>                 … 初回。対象 + 前後 per 件ずつ
    id + dir=newer&cursor=L,I     … その端よりさらに新しい per 件
    id + dir=older&cursor=L,I     … その端よりさらに古い per 件
    （カーソルは created_local と posts.id の組。同時刻の取りこぼしを防ぐ）
    """
    try:
        pid = int(request.args.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "id が不正です"}), 400
    per = min(50, max(1, int(request.args.get("per", 10))))
    direction = request.args.get("dir")
    con = common.connect()
    try:
        row = con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        if not row:
            return jsonify({"error": "投稿が見つかりません"}), 404
        target = dict(row)
        svc, acct = target["service"], target["account"]

        def fetch(newer, loc, rid, n):
            if newer:
                rows = con.execute(
                    "SELECT * FROM posts WHERE service=? AND account=? "
                    "AND (created_local > ? OR (created_local = ? AND id > ?)) "
                    "ORDER BY created_local ASC, id ASC LIMIT ?",
                    (svc, acct, loc, loc, rid, n)).fetchall()
                rows = rows[::-1]          # 表示は常に新しい順
            else:
                rows = con.execute(
                    "SELECT * FROM posts WHERE service=? AND account=? "
                    "AND (created_local < ? OR (created_local = ? AND id < ?)) "
                    "ORDER BY created_local DESC, id DESC LIMIT ?",
                    (svc, acct, loc, loc, rid, n)).fetchall()
            return attach_details(con, [dict(r) for r in rows])

        if direction in ("newer", "older"):
            cur = (request.args.get("cursor") or "").rsplit(",", 1)
            if len(cur) != 2 or not cur[1].isdigit():
                return jsonify({"error": "cursor が不正です"}), 400
            posts = fetch(direction == "newer", cur[0], int(cur[1]), per)
            return jsonify({"posts": posts})

        newer = fetch(True, target["created_local"], pid, per)
        older = fetch(False, target["created_local"], pid, per)
        return jsonify({
            "target": attach_details(con, [target])[0],
            "newer": newer,
            "older": older,
        })
    finally:
        con.close()


@app.route("/media/<path:filename>")
def media(filename):
    return send_from_directory(common.MEDIA_DIR, filename, conditional=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    try:
        cfg = common.load_config()
    except SystemExit:
        cfg = {}
    port = args.port or (cfg.get("port") if isinstance(cfg, dict) else None) or 5089
    url = f"http://127.0.0.1:{port}/"
    print(f"統合SNSアーカイブ ビューア: {url}")
    print("終了するには Ctrl+C（またはこのウィンドウを閉じる）")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=port, threaded=True)


if __name__ == "__main__":
    main()
