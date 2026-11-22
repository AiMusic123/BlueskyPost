#!/usr/bin/env python3
"""
Bluesky 自動投稿bot

POSTフォルダの Post1.txt, Post2.txt ... を1回の実行につき1件ずつ、Bluesky に投稿します。
外部ライブラリ不要（Python標準ライブラリのみ）。

環境変数:
  BSKY_HANDLE        投稿するアカウントのハンドル (例: alexaimusic.bsky.social)
  BSKY_APP_PASSWORD  Bluesky の「アプリパスワード」（通常のログインパスワードは使わない）
  POST_MODE          sequential (上から順番, デフォルト) / random (ランダム)
  POSTS_DIR          投稿ファイルを入れるフォルダ (デフォルト: POST)
  STATE_FILE         最後に投稿した番号を記録するファイル (デフォルト: state.json)
  LOOP               true なら最後まで投稿したら最初に戻る (デフォルト: true)
  DRY_RUN            true なら実際には投稿せず内容を表示するだけ
  BSKY_PDS           接続先サーバー (デフォルト: https://bsky.social)
"""

import json
import os
import random
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone

PDS = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/")
POSTS_DIR = os.environ.get("POSTS_DIR", "POST")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
POST_MODE = os.environ.get("POST_MODE", "sequential").lower()
LOOP = os.environ.get("LOOP", "true").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

MAX_GRAPHEMES = 300  # Bluesky の1投稿あたりの文字数上限

URL_RE = re.compile(r"https?://[^\s　]+")
# 日本語ハッシュタグにも対応（#の後ろ、空白か次の#まで）
TAG_RE = re.compile(r"(?:^|(?<=[\s　]))[#＃]([^\s　#＃]+)")
TRAILING_PUNCT = ".,;:!?)]}>、。！？」』）】"


# ---------- 投稿ファイルの読み込み ----------

def load_posts(folder):
    """POSTフォルダ内の Post1.txt, Post2.txt ... を番号順に読み込む。
    1ファイル = 1投稿。戻り値は [(番号, ファイル名, 本文), ...]"""
    posts = []
    if not os.path.isdir(folder):
        return posts
    for name in os.listdir(folder):
        m = re.fullmatch(r"post(\d+)\.txt", name, re.IGNORECASE)
        if not m:
            continue
        with open(os.path.join(folder, name), encoding="utf-8-sig") as f:
            text = f.read().strip()
        if text:
            posts.append((int(m.group(1)), name, text))
    posts.sort()
    return posts


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def grapheme_len(text):
    """おおよその書記素（見た目の文字）数。結合文字や異体字セレクタ等は数えない。"""
    count = 0
    for ch in text:
        if unicodedata.combining(ch) or ch in "‍︎️" or 0x1F3FB <= ord(ch) <= 0x1F3FF:
            continue
        count += 1
    return count


# ---------- リンク・ハッシュタグを有効にする (facets) ----------

def _byte_range(text, start, end):
    b_start = len(text[:start].encode("utf-8"))
    b_end = b_start + len(text[start:end].encode("utf-8"))
    return {"byteStart": b_start, "byteEnd": b_end}


def build_facets(text):
    facets = []
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(TRAILING_PUNCT)
        start, end = m.start(), m.start() + len(url)
        facets.append({
            "index": _byte_range(text, start, end),
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
        })
    for m in TAG_RE.finditer(text):
        tag = m.group(1).rstrip(TRAILING_PUNCT)
        if not tag or tag.isdigit():
            continue
        start = m.start(1) - 1  # '#' を含める
        end = m.start(1) + len(tag)
        facets.append({
            "index": _byte_range(text, start, end),
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}],
        })
    return facets


# ---------- Bluesky API ----------

def xrpc(method, body, token=None):
    req = urllib.request.Request(
        f"{PDS}/xrpc/{method}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} failed: HTTP {e.code} {detail}") from None


def post_to_bluesky(handle, app_password, text):
    session = xrpc("com.atproto.server.createSession",
                   {"identifier": handle, "password": app_password})
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "langs": ["ja"],
    }
    facets = build_facets(text)
    if facets:
        record["facets"] = facets
    return xrpc("com.atproto.repo.createRecord", {
        "repo": session["did"],
        "collection": "app.bsky.feed.post",
        "record": record,
    }, token=session["accessJwt"])


# ---------- メイン ----------

def main():
    posts = load_posts(POSTS_DIR)
    if not posts:
        print(f"{POSTS_DIR} フォルダに Post〇.txt がありません。")
        return 0

    state = load_state()
    last = state.get("last_number")  # 最後に投稿したファイルの番号

    if POST_MODE == "random":
        choices = [p for p in posts if p[0] != last] or posts
        number, name, text = random.choice(choices)
    else:
        # 前回の番号より大きい中で一番小さいものを選ぶ（途中でファイルを足しても大丈夫）
        rest = [p for p in posts if last is None or p[0] > last]
        if not rest:
            if not LOOP:
                print("すべての投稿が完了しました（LOOP=false のため終了）。")
                return 0
            rest = posts
        number, name, text = rest[0]

    length = grapheme_len(text)
    print(f"{name}（全{len(posts)}件中） {length}文字")
    print(text)
    print("-" * 40)

    if length > MAX_GRAPHEMES:
        print(f"エラー: {MAX_GRAPHEMES}文字を超えています（{length}文字）。{name} はスキップします。")
        state.update({"last_number": number})
        save_state(state)
        return 1

    if DRY_RUN:
        print("DRY_RUN のため投稿しませんでした。")
        return 0

    handle = os.environ.get("BSKY_HANDLE")
    password = os.environ.get("BSKY_APP_PASSWORD")
    if not handle or not password:
        print("エラー: BSKY_HANDLE と BSKY_APP_PASSWORD を設定してください。")
        return 1

    result = post_to_bluesky(handle, password, text)
    print(f"投稿しました: {result.get('uri')}")

    state = {
        "last_number": number,
        "last_file": name,
        "last_posted_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
