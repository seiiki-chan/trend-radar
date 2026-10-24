# src/overseas.py
import os
import re
import time
import xml.etree.ElementTree as ET

import requests

REPO = os.environ.get("GITHUB_REPOSITORY", "local")
USER_AGENT = f"trend-radar/1.0 (+https://github.com/{REPO})"
TIMEOUT = 15

# 要確認: Google Trends RSS は非公式。新旧2つのURLを順に試す
GT_URLS = [
    "https://trends.google.com/trending/rss?geo={geo}",
    "https://trends.google.com/trends/trendingsearches/daily/rss?geo={geo}",
]
HN_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"
WIKI_API = "https://{lang}.wikipedia.org/w/api.php"

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})
_MISS = object()  # 通信失敗（キャッシュしない）と「見つからない」を区別する目印


def _get(url, params=None, retries=3):
    last = "unknown"
    for i in range(retries):
        try:
            r = _session.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 401, 403, 404):
                break
        except requests.RequestException as e:
            last = type(e).__name__
        time.sleep(1.5 * (i + 1))
    print(f"[radar] WARN get failed: {url.split('?')[0]} ({last})", flush=True)
    return None


def parse_traffic(s):
    """'200+' '2K+' '1M+' などを数値にする。"""
    if not s:
        return 0
    m = re.match(r"\s*([\d,\.]+)\s*([KkMm]?)", s)
    if not m:
        return 0
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    mult = {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]
    return int(n * mult)


def get_google_trends(geo, limit=20):
    """[(タイトル, 検索トラフィック概数)] を返す。失敗時は空リスト。"""
    for tmpl in GT_URLS:
        r = _get(tmpl.format(geo=geo), retries=2)
        if r is None:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            traffic = 0
            for child in item:
                if child.tag.endswith("approx_traffic"):
                    traffic = parse_traffic(child.text)
                    break
            items.append((title, traffic))
            if len(items) >= limit:
                break
        if items:
            return items
    return []


def get_jp_trends():
    """日本のGoogle Trendsに出ている語の集合（casefold済み）。"""
    return {t.casefold() for t, _ in get_google_trends("JP", 50)}


def get_hacker_news(min_score=300, max_age_hours=8, scan=40):
    r = _get(HN_TOP)
    if r is None:
        return []
    try:
        ids = r.json()[:scan]
    except (ValueError, TypeError):
        return []
    now = time.time()
    out = []
    fails = 0
    for i in ids:
        rr = _get(HN_ITEM.format(i), retries=2)
        if rr is None:
            fails += 1
            if fails >= 5:
                break
            continue
        fails = 0
        try:
            it = rr.json()
        except ValueError:
            continue
        if not isinstance(it, dict) or it.get("type") != "story":
            continue
        score = it.get("score", 0)
        ts = it.get("time", 0)
        title = it.get("title")
        if title and score >= min_score and now - ts <= max_age_hours * 3600:
            out.append({"id": it.get("id"), "title": title, "score": score})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def _wiki_ja(term, lang):
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": term, "gsrlimit": "1",
        "prop": "langlinks", "lllang": "ja", "redirects": "1",
    }
    r = _get(WIKI_API.format(lang=lang), params=params, retries=2)
    if r is None:
        return _MISS
    try:
        data = r.json()
    except ValueError:
        return _MISS
    pages = (data.get("query") or {}).get("pages") or []
    if not pages:
        return None
    links = pages[0].get("langlinks") or []
    if not links:
        return None
    title = str(links[0].get("title", ""))
    title = re.sub(r"\s*[\(（].*?[\)）]\s*$", "", title).strip()  # 括弧付き曖昧さ回避を除去
    return title or None


def to_ja(term, lang, cache):
    """外国語の語を日本語タイトルへ。結果は cache に保存。通信失敗時は保存しない。"""
    key = f"{lang}:{term}"
    if key in cache:
        return cache[key]
    time.sleep(0.2)
    res = _wiki_ja(term, lang)
    if res is _MISS:
        return None
    cache[key] = res
    return res


def collect_overseas(geos, cache, top=20):
    """海外の急上昇語を集約し、日本語名を付けて返す。"""
    cands = {}
    for g in geos:
        geo = g["geo"]
        lang = g.get("wiki", "en")
        for title, traffic in get_google_trends(geo, top):
            k = title.strip().casefold()
            c = cands.setdefault(k, {"term": title.strip(), "geos": set(),
                                     "traffic": 0, "lang": lang})
            c["geos"].add(geo)
            c["traffic"] = max(c["traffic"], traffic)
    out = []
    for c in cands.values():
        ja = to_ja(c["term"], c["lang"], cache)
        out.append({"term": c["term"], "geos": sorted(c["geos"]),
                    "traffic": c["traffic"], "ja": ja})
    return out
