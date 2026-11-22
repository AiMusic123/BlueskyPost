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
  LINK_CARD          true なら本文の最初のURLをサムネ付きカードにする (デフォルト: true)
  BSKY_PDS           接続先サーバー (デフォルト: https://bsky.social)
"""

import html
import json
import os
import random
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PDS = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/")
POSTS_DIR = os.environ.get("POSTS_DIR", "POST")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
POST_MODE = os.environ.get("POST_MODE", "sequential").lower()
LOOP = os.environ.get("LOOP", "true").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
LINK_CARD = os.environ.get("LINK_CARD", "true").lower() == "true"

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


# ---------- リンクカード（サムネ付きプレビュー） ----------

UA = "Mozilla/5.0 (compatible; BlueskyPostBot/1.0; +https://bsky.app)"
MAX_THUMB_BYTES = 950_000  # Bluesky の画像上限 (約1MB) より少し小さめ
YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def http_get(url, max_bytes=3_000_000):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read(max_bytes), res.headers.get("Content-Type", "")


def _meta(html_text, key):
    """<meta property="og:title" content="..."> のような値を取り出す"""
    for pat in (
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(key)}["\']',
    ):
        m = re.search(pat, html_text, re.IGNORECASE)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def fetch_link_info(url):
    """URL からタイトル・説明・サムネ画像URLを取得する"""
    info = {"title": "", "description": "", "image": ""}

    yt = YT_ID_RE.search(url)
    if yt:
        vid = yt.group(1)
        try:
            data, _ = http_get("https://www.youtube.com/oembed?format=json&url="
                               + urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}", safe=""))
            info["title"] = json.loads(data).get("title", "")
        except Exception as e:
            print(f"  YouTubeのタイトル取得に失敗: {e}")
        info["image"] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        # 説明文はページから取れれば使う
        try:
            data, _ = http_get(url)
            page = data.decode("utf-8", errors="replace")
            info["description"] = _meta(page, "og:description")
            info["title"] = info["title"] or _meta(page, "og:title")
            img = _meta(page, "og:image")
            if img:
                info["image"] = img
        except Exception:
            pass
        return info

    data, ctype = http_get(url)
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        charset = m.group(1)
    page = data.decode(charset, errors="replace")
    info["title"] = _meta(page, "og:title") or _meta(page, "twitter:title")
    if not info["title"]:
        t = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
        info["title"] = html.unescape(t.group(1)).strip() if t else ""
    info["description"] = _meta(page, "og:description") or _meta(page, "description")
    img = _meta(page, "og:image") or _meta(page, "twitter:image")
    if img:
        info["image"] = urllib.parse.urljoin(url, img)
    return info


def build_link_card(url, token):
    """app.bsky.embed.external（リンクカード）を作る。失敗したら None"""
    try:
        info = fetch_link_info(url)
    except Exception as e:
        print(f"  リンク先の情報取得に失敗したため、カードなしで投稿します: {e}")
        return None

    external = {
        "uri": url,
        "title": info["title"][:300] or url,
        "description": info["description"][:1000],
    }

    if info["image"]:
        try:
            img, ctype = http_get(info["image"], max_bytes=MAX_THUMB_BYTES + 1)
            if len(img) > MAX_THUMB_BYTES:
                print("  サムネ画像が大きすぎるため、画像なしのカードにします。")
            else:
                mime = ctype.split(";")[0].strip() or "image/jpeg"
                blob = upload_blob(img, mime, token)
                external["thumb"] = blob
        except Exception as e:
            print(f"  サムネ画像の取得に失敗しました: {e}")

    print(f"  リンクカード: {external['title']}{' (サムネあり)' if 'thumb' in external else ''}")
    return {"$type": "app.bsky.embed.external", "external": external}


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


def upload_blob(data, mime, token):
    req = urllib.request.Request(
        f"{PDS}/xrpc/com.atproto.repo.uploadBlob",
        data=data,
        headers={"Content-Type": mime, "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))["blob"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"uploadBlob failed: HTTP {e.code} {detail}") from None


def post_to_bluesky(handle, app_password, text):
    session = xrpc("com.atproto.server.createSession",
                   {"identifier": handle, "password": app_password})
    token = session["accessJwt"]
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "langs": ["ja"],
    }
    facets = build_facets(text)
    if facets:
        record["facets"] = facets

    # 本文の最初のURLをリンクカードにする
    if LINK_CARD:
        urls = [f["features"][0]["uri"] for f in facets
                if f["features"][0]["$type"].endswith("#link")]
        if urls:
            card = build_link_card(urls[0], token)
            if card:
                record["embed"] = card

    return xrpc("com.atproto.repo.createRecord", {
        "repo": session["did"],
        "collection": "app.bsky.feed.post",
        "record": record,
    }, token=token)


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
