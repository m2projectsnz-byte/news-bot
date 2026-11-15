"""日本とNZのニュースをBlueskyに自動投稿するボット（見出し＋リンクのみ）。"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

# ====== 設定（ここを書き換えるだけで調整できます） ======
FEEDS = [
    {
        "name": "NHK 主要ニュース",
        "url": "https://www3.nhk.or.jp/rss/news/cat0.xml",
        "prefix": "🇯🇵",
        "lang": "ja",
    },
    {
        "name": "RNZ ニュース",
        "url": "https://www.rnz.co.nz/rss/national.xml",
        "prefix": "🇳🇿",
        "lang": "en",
    },
]
MAX_POSTS_PER_FEED = 2   # 1回の実行で、1つのフィードから投稿する最大件数
STATE_FILE = "posted.json"  # 投稿済みURLの記録（重複投稿防止）
MAX_TEXT = 300           # Blueskyの文字数上限
# =========================================================

PDS = "https://bsky.social"
UA = {"User-Agent": "news-bot/1.0 (+https://bsky.app)"}


def fetch_items(url):
    """RSS/Atomから (title, link) のリストを新しい順で返す。"""
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):  # RSS 2.0
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if title and link:
            items.append((title, link))
    if not items:  # Atom
        ns = "{http://www.w3.org/2005/Atom}"
        for e in root.iter(ns + "entry"):
            title = (e.findtext(ns + "title") or "").strip()
            l = e.find(ns + "link")
            link = (l.get("href") if l is not None else "") or ""
            if title and link:
                items.append((title, link.strip()))
    return items


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_state(posted):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(posted[-500:], f, ensure_ascii=False, indent=1)


def build_post(prefix, title, link):
    """本文とリンクのfacet（クリックできるリンク）を作る。"""
    title = re.sub(r"\s+", " ", title)
    budget = MAX_TEXT - len(prefix) - 1 - 1 - len(link)  # 空白と改行ぶん
    if len(title) > budget:
        title = title[: max(budget - 1, 0)] + "…"
    head = f"{prefix} {title}\n"
    text = head + link
    start = len(head.encode("utf-8"))
    end = start + len(link.encode("utf-8"))
    facets = [
        {
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": link}],
        }
    ]
    return text, facets


def login():
    r = requests.post(
        f"{PDS}/xrpc/com.atproto.server.createSession",
        json={
            "identifier": os.environ["BSKY_HANDLE"],
            "password": os.environ["BSKY_APP_PASSWORD"],
        },
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    return d["accessJwt"], d["did"]


def post(jwt, did, text, facets, lang):
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "facets": facets,
        "langs": [lang],
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    r = requests.post(
        f"{PDS}/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"repo": did, "collection": "app.bsky.feed.post", "record": record},
        timeout=30,
    )
    r.raise_for_status()


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    posted = load_state()
    seen = set(posted)
    jwt = did = None

    for feed in FEEDS:
        try:
            items = fetch_items(feed["url"])
        except Exception as e:
            print(f"[skip] {feed['name']}: 取得失敗 {e}", file=sys.stderr)
            continue

        new = [(t, l) for t, l in items if l not in seen][:MAX_POSTS_PER_FEED]
        print(f"{feed['name']}: 新着 {len(new)} 件")
        for title, link in reversed(new):  # 古い順に投稿
            text, facets = build_post(feed["prefix"], title, link)
            if dry:
                print("--- DRY RUN ---\n" + text)
            else:
                if jwt is None:
                    jwt, did = login()
                try:
                    post(jwt, did, text, facets, feed["lang"])
                    print("投稿:", title)
                except Exception as e:
                    print(f"[error] 投稿失敗 {e}", file=sys.stderr)
                    continue
            posted.append(link)
            seen.add(link)

        # 投稿しなかった古い記事も「既読」にして、後でまとめて流れないようにする
        for _, l in items:
            if l not in seen:
                posted.append(l)
                seen.add(l)

    save_state(posted)


if __name__ == "__main__":
    main()
