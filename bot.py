"""日本とNZのニュースをBlueskyに自動投稿するボット（見出し＋リンクのみ）。"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

# ====== 設定（ここを書き換えるだけで調整できます） ======
FEEDS = [
    # ---- 日本（日本語） ----
    {"name": "NHK 主要", "url": "https://www3.nhk.or.jp/rss/news/cat0.xml", "prefix": "🇯🇵", "lang": "ja"},
    {"name": "NHK 社会", "url": "https://www3.nhk.or.jp/rss/news/cat1.xml", "prefix": "🇯🇵", "lang": "ja"},
    {"name": "NHK 政治", "url": "https://www3.nhk.or.jp/rss/news/cat4.xml", "prefix": "🇯🇵", "lang": "ja"},
    {"name": "NHK 経済", "url": "https://www3.nhk.or.jp/rss/news/cat5.xml", "prefix": "🇯🇵", "lang": "ja"},
    {"name": "NHK 国際", "url": "https://www3.nhk.or.jp/rss/news/cat6.xml", "prefix": "🇯🇵", "lang": "ja"},
    {"name": "Yahoo 主要", "url": "https://news.yahoo.co.jp/rss/topics/top-picks.xml", "prefix": "🇯🇵", "lang": "ja"},
    # Yahoo のカテゴリ別（URLが違う場合はログに [skip] と出るだけで、他には影響しません）
    {"name": "Yahoo 国内", "url": "https://news.yahoo.co.jp/rss/topics/domestic.xml", "prefix": "🇯🇵", "lang": "ja"},
    {"name": "Yahoo 国際", "url": "https://news.yahoo.co.jp/rss/topics/world.xml", "prefix": "🇯🇵", "lang": "ja"},
    {"name": "Yahoo 経済", "url": "https://news.yahoo.co.jp/rss/topics/business.xml", "prefix": "🇯🇵", "lang": "ja"},
    # ---- ニュージーランド（英語） ----
    # メイン: 1News（ニュース用サイトマップ形式）
    {"name": "1News", "url": "https://www.1news.co.nz/arc/outboundfeeds/news-sitemap/?outputType=xml",
     "type": "newssitemap", "prefix": "🇳🇿", "lang": "en", "max": 4},
    # サブ: RNZ
    {"name": "RNZ 国内", "url": "https://www.rnz.co.nz/rss/national.xml", "prefix": "🇳🇿", "lang": "en", "max": 2},
]
MAX_POSTS_PER_FEED = 3   # 1回の実行で、1つのフィードから投稿する最大件数
STATE_FILE = "posted.json"  # 投稿済みURLの記録（重複投稿防止）
STATE_KEEP = 5000        # 記録するURLの最大数（少ないと古い記事が再投稿されます）
MAX_AGE_HOURS = 72       # これより古い記事は投稿しない（更新が止まったフィード対策）
MAX_TEXT = 300           # Blueskyの文字数上限
# =========================================================

PDS = "https://bsky.social"
UA = {"User-Agent": "news-bot/1.0 (+https://bsky.app)"}


def parse_date(s):
    """RSSのpubDate（RFC822）やISO形式の日時を、タイムゾーン付きで返す。失敗したらNone。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def is_fresh(d):
    """日付が不明なものは通し、MAX_AGE_HOURSより古いものは除外する。"""
    if d is None:
        return True
    return datetime.now(timezone.utc) - d <= timedelta(hours=MAX_AGE_HOURS)


def fetch_news_sitemap(url):
    """Googleニュース形式のサイトマップから (title, link) を新しい順で返す。"""
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    sm = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    nw = "{http://www.google.com/schemas/sitemap-news/0.9}"
    rows = []
    for u in root.iter(sm + "url"):
        link = (u.findtext(sm + "loc") or "").strip()
        title = (u.findtext(f"{nw}news/{nw}title") or "").strip()
        date = u.findtext(f"{nw}news/{nw}publication_date") or ""
        if link and title:
            rows.append((date, title, link))
    rows.sort(reverse=True)
    return [(t, l, parse_date(d)) for d, t, l in rows]


def fetch_items(url):
    """RSS/Atomから (title, link) のリストを新しい順で返す。"""
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):  # RSS 2.0
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        d = parse_date(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"))
        if title and link:
            items.append((title, link, d))
    if not items:  # Atom
        ns = "{http://www.w3.org/2005/Atom}"
        for e in root.iter(ns + "entry"):
            title = (e.findtext(ns + "title") or "").strip()
            l = e.find(ns + "link")
            link = (l.get("href") if l is not None else "") or ""
            d = parse_date(e.findtext(ns + "updated") or e.findtext(ns + "published"))
            if title and link:
                items.append((title, link.strip(), d))
    return items


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_state(posted):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(posted[-STATE_KEEP:], f, ensure_ascii=False, indent=1)


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
            if feed.get("type") == "newssitemap":
                items = fetch_news_sitemap(feed["url"])
            else:
                items = fetch_items(feed["url"])
        except Exception as e:
            print(f"[skip] {feed['name']}: 取得失敗 {e}", file=sys.stderr)
            continue

        limit = feed.get("max", MAX_POSTS_PER_FEED)
        dates = [d for _, _, d in items if d is not None]
        newest = max(dates).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if dates else "不明"
        stale = sum(1 for _, _, d in items if not is_fresh(d))
        new = [(t, l) for t, l, d in items if l not in seen and is_fresh(d)][:limit]
        print(f"{feed['name']}: 新着 {len(new)} 件 / 最新記事の日時 {newest} / 古くて除外 {stale} 件")
        if dates and stale == len(items):
            print(f"[警告] {feed['name']}: 更新が止まっている可能性があります", file=sys.stderr)
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
        for _, l, _d in items:
            if l not in seen:
                posted.append(l)
                seen.add(l)

    save_state(posted)


if __name__ == "__main__":
    main()
