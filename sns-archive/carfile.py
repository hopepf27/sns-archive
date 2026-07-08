# -*- coding: utf-8 -*-
"""
carfile.py — Bluesky公式エクスポート(.car)の最小読み取り実装

CAR(v1) コンテナ + DAG-CBOR + atproto MST(Merkle Search Tree) を
外部ライブラリなしで読み、リポジトリ内の全レコードを列挙する。

blobレコード内のCIDリンク(タグ42)は listRecords API と同じ
{"$link": "b..."} 形式に変換するので、API取得と同じ後処理コードが使える。
"""
import base64
import struct


class CarError(Exception):
    pass


# ---------------- varint / CID ----------------

def _uvarint(buf, i):
    """unsigned LEB128。(値, 次の位置) を返す。"""
    shift = val = 0
    while True:
        if i >= len(buf):
            raise CarError("varintが途中で終わっています")
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 63:
            raise CarError("varintが長すぎます")


def cid_str(raw: bytes) -> str:
    """CIDバイト列 → multibase base32 文字列（'b' + base32小文字）。"""
    return "b" + base64.b32encode(raw).decode("ascii").lower().rstrip("=")


def _read_cid(buf, i):
    """ブロック先頭のCIDを読む。(cid文字列, 次の位置)"""
    start = i
    if buf[i] == 0x12 and buf[i + 1] == 0x20:      # CIDv0 (dag-pb sha256)
        return cid_str(buf[start:start + 34]), start + 34
    _ver, i = _uvarint(buf, i)                     # version (=1)
    _codec, i = _uvarint(buf, i)                   # 0x71 dag-cbor / 0x55 raw
    _hcode, i = _uvarint(buf, i)                   # ハッシュ種別
    hlen, i = _uvarint(buf, i)                     # ダイジェスト長
    i += hlen
    return cid_str(buf[start:i]), i


# ---------------- DAG-CBOR デコーダ ----------------

def _cbor(buf, i):
    """(値, 次の位置)。タグ42(CID)は {"$link": cid文字列} に変換する。"""
    if i >= len(buf):
        raise CarError("CBORが途中で終わっています")
    ib = buf[i]
    i += 1
    mt, ai = ib >> 5, ib & 0x1F

    if ai < 24:
        arg = ai
    elif ai == 24:
        arg = buf[i]; i += 1
    elif ai == 25:
        arg = int.from_bytes(buf[i:i + 2], "big"); i += 2
    elif ai == 26:
        arg = int.from_bytes(buf[i:i + 4], "big"); i += 4
    elif ai == 27:
        arg = int.from_bytes(buf[i:i + 8], "big"); i += 8
    else:
        raise CarError(f"未対応のCBOR additional info: {ai}")

    if mt == 0:                                    # 非負整数
        return arg, i
    if mt == 1:                                    # 負整数
        return -1 - arg, i
    if mt == 2:                                    # バイト列
        return bytes(buf[i:i + arg]), i + arg
    if mt == 3:                                    # 文字列
        return buf[i:i + arg].decode("utf-8"), i + arg
    if mt == 4:                                    # 配列
        out = []
        for _ in range(arg):
            v, i = _cbor(buf, i)
            out.append(v)
        return out, i
    if mt == 5:                                    # マップ
        out = {}
        for _ in range(arg):
            k, i = _cbor(buf, i)
            v, i = _cbor(buf, i)
            out[k] = v
        return out, i
    if mt == 6:                                    # タグ
        v, i = _cbor(buf, i)
        if arg == 42:                              # CIDリンク
            if not isinstance(v, bytes) or not v or v[0] != 0:
                raise CarError("不正なCIDリンク")
            return {"$link": cid_str(v[1:])}, i    # 先頭0x00(identity)を除去
        return v, i
    if mt == 7:
        if ai == 20:
            return False, i
        if ai == 21:
            return True, i
        if ai == 22:
            return None, i
        if ai == 27:                               # float64
            return struct.unpack(">d", bytes(buf[i - 8:i]))[0], i
        raise CarError(f"未対応のCBOR simple値: {ai}")
    raise CarError(f"未対応のCBOR major type: {mt}")


def cbor_decode(buf):
    v, _ = _cbor(buf, 0)
    return v


# ---------------- CAR 読み取り ----------------

def read_car(data: bytes):
    """CARファイル全体 → (roots(list[str]), blocks{cid文字列: bytes})"""
    hlen, i = _uvarint(data, 0)
    header = cbor_decode(data[i:i + hlen])
    i += hlen
    if header.get("version") != 1:
        raise CarError(f"未対応のCARバージョン: {header.get('version')}")
    roots = [r["$link"] if isinstance(r, dict) else r
             for r in header.get("roots") or []]
    blocks = {}
    n = len(data)
    while i < n:
        blen, i = _uvarint(data, i)
        end = i + blen
        if end > n:
            raise CarError("ブロックが途中で終わっています")
        cid, j = _read_cid(data, i)
        blocks[cid] = bytes(data[j:end])
        i = end
    return roots, blocks


# ---------------- MST 走査 ----------------

def _walk_mst(blocks, node_cid, prev_key=b"", out=None):
    if out is None:
        out = []
    raw = blocks.get(node_cid)
    if raw is None:
        return out            # 部分CARでは欠けることがある
    node = cbor_decode(raw)
    left = node.get("l")
    if isinstance(left, dict):
        _walk_mst(blocks, left["$link"], prev_key, out)
    key = prev_key
    for e in node.get("e") or []:
        p = e.get("p") or 0
        k = e.get("k") or b""
        key = key[:p] + (k if isinstance(k, bytes) else bytes(k))
        v = e.get("v")
        if isinstance(v, dict):
            out.append((key.decode("ascii", "replace"), v["$link"]))
        t = e.get("t")
        if isinstance(t, dict):
            _walk_mst(blocks, t["$link"], key, out)
    return out


def repo_records(data: bytes):
    """CARバイト列 → (did, [(collection, rkey, レコードdict), ...])"""
    roots, blocks = read_car(data)
    if not roots:
        raise CarError("ルートCIDがありません")
    commit = cbor_decode(blocks[roots[0]])
    did = commit.get("did")
    data_link = commit.get("data")
    if not isinstance(data_link, dict):
        raise CarError("コミットにMSTルートがありません")
    records = []
    for key, cid in _walk_mst(blocks, data_link["$link"]):
        raw = blocks.get(cid)
        if raw is None:
            continue
        try:
            val = cbor_decode(raw)
        except CarError:
            continue
        if "/" in key and isinstance(val, dict):
            coll, rk = key.split("/", 1)
            records.append((coll, rk, val))
    return did, records
