# src/bluesky.py
import json
import math
import re
import time
import unicodedata

import websocket

from overseas import USER_AGENT

# 要確認: Jetstream 公開インスタンスのURL（公式ドキュメントで確認）
JETSTREAM_URLS = [
    "wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post",
    "wss://jetstream1.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post",
    "wss://jetstream2.us-west.bsky.network/subscribe?wantedCollections=app.bsky.feed.post",
    "wss://jetstream1.us-west.bsky.network/subscribe?wantedCollections=app.bsky.feed.post",
]

KANA = re.compile(r"[\u3040-\u30ff]")
HIRAGANA_ONLY = re.compile(r"^[\u3040-\u309f]+$")
URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"@[\w\.\-]+")
HASHTAG_RE = re.compile(r"#([^\s#、。！？!?,.，（）()「」『』【】\[\]]{2,40})")
STOP = {"今日", "明日", "昨日", "今年", "去年", "最近", "最初", "最後", "自分", "みんな",
        "全部", "今回", "前回", "毎日", "毎回", "世界", "普通", "必要", "問題", "理由"}
FLOOR = 0.05  # 平常値の下限（1000投稿あたりのユニークユーザー数）


def make_tagger():
    import fugashi
    return fugashi.Tagger()


def _norm(s):
    return unicodedata.normalize("NFKC", s).casefold().strip()


def extract_terms(text, tagger):
    """投稿1件から語の集合を取り出す（ハッシュタグ + 名詞）。"""
    text = unicodedata.normalize("NFKC", text)[:600]
    terms = set()
    for m in HASHTAG_RE.finditer(text):
        terms.add(m.group(1).casefold())
    body = URL_RE.sub(" ", text)
    body = MENTION_RE.sub(" ", body)
    body = HASHTAG_RE.sub(" ", body)
    for w in tagger(body):
        f = w.feature
        if getattr(f, "pos1", None) != "名詞":
            continue
        if getattr(f, "pos2", None) not in ("普通名詞", "固有名詞"):
            continue
        s = w.surface.strip()
        if len(s) < 2 or s.isdigit() or s in STOP:
            continue
        if HIRAGANA_ONLY.match(s) and len(s) <= 2:
            continue
        terms.add(s.casefold())
    return terms


def _is_japanese(rec):
    langs = rec.get("langs")
    if langs:
        return any(str(x).lower().startswith("ja") for x in langs)
    return bool(KANA.search(rec.get("text", "")))


def sample(seconds, targets, tagger):
    """Jetstreamから日本語投稿を取得し、語ごとのユニークユーザー数を集計する。
    投稿本文・DIDはメモリ上でのみ扱い、保存しない。"""
    deadline = time.time() + seconds
    norm_targets = {_norm(t): t for t in targets}
    term_users = {}
    target_users = {t: set() for t in targets}
    total = 0
    finished = False

    for url in JETSTREAM_URLS:
        if finished or time.time() >= deadline:
            break
        host = url.split("/")[2]
        ws = None
        try:
            ws = websocket.create_connection(
                url, timeout=10, header=[f"User-Agent: {USER_AGENT}"])
            ws.settimeout(10)
            print(f"[radar] bluesky: connected {host}", flush=True)
            while time.time() < deadline:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise websocket.WebSocketConnectionClosedException("empty")
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if ev.get("kind") != "commit":
                    continue
                c = ev.get("commit") or {}
                if c.get("operation") != "create" or c.get("collection") != "app.bsky.feed.post":
                    continue
                rec = c.get("record") or {}
                text = rec.get("text")
                if not isinstance(text, str) or not text or not _is_japanese(rec):
                    continue
                did = ev.get("did")
                if not did:
                    continue
                uid = hash(did)  # プロセスごとに値が変わる。保存しない
                total += 1
                try:
                    for t in extract_terms(text, tagger):
                        term_users.setdefault(t, set()).add(uid)
                except Exception:
                    pass
                nt = _norm(text)
                for k, orig in norm_targets.items():
                    if k and k in nt:
                        target_users[orig].add(uid)
            finished = True
        except (websocket.WebSocketException, OSError) as e:
            print(f"[radar] WARN bluesky {host}: {type(e).__name__}", flush=True)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    return {
        "posts": total,
        "counts": {t: len(u) for t, u in term_users.items() if len(u) >= 2},
        "target": {t: len(u) for t, u in target_users.items()},
    }


def find_rising(counts, total, base_terms, min_users, min_ratio):
    """平常値(EMA)に対する比で急上昇語を判定する。"""
    out = []
    for term, users in counts.items():
        if users < min_users:
            continue
        normal = max(float(base_terms.get(term, 0.0)), FLOOR)
        expected = normal * total / 1000.0
        ratio = users / expected
        if ratio < min_ratio:
            continue
        out.append({"term": term, "users": users, "ratio": ratio,
                    "score": ratio * math.log(users)})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def update_baseline(base_terms, counts, total, alpha=0.1, max_terms=8000):
    """EMA更新。0.05未満は破棄。新語は2人以上出現したものだけ観測値で初期化。"""
    new = {}
    for term, old in base_terms.items():
        rate = counts.get(term, 0) / total * 1000.0
        v = alpha * rate + (1 - alpha) * float(old)
        if v >= FLOOR:
            new[term] = round(v, 4)
    for term, users in counts.items():
        if term in base_terms:
            continue
        rate = users / total * 1000.0
        if rate >= FLOOR:
            new[term] = round(rate, 4)
    if len(new) > max_terms:
        top = sorted(new.items(), key=lambda kv: kv[1], reverse=True)[:max_terms]
        new = dict(top)
    return new
