# src/main.py
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import bluesky
import notify
import overseas
import state
from notify import sanitize

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "keywords": [], "collect_seconds": 240, "min_users": 6, "min_ratio": 5.0,
    "warmup_runs": 24, "top_n": 5, "renotify_hours": 12, "only_related": False,
    "landing_users": 3, "min_sample_posts": 300, "hn_min_score": 300,
    "hn_max_age_hours": 8,
    "overseas_geos": [{"geo": "US", "wiki": "en"}, {"geo": "GB", "wiki": "en"},
                      {"geo": "KR", "wiki": "ko"}, {"geo": "TW", "wiki": "zh"}],
}

_ASCII_KW = re.compile(r"[A-Za-z0-9]+")


def log(msg):
    print(f"[radar] {msg}", flush=True)


def is_dry_run():
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8-sig") as f:
        loaded = yaml.safe_load(f) or {}
    return {**DEFAULTS, **loaded}


def related(text, keywords):
    """自社キーワードに一致するか。
    - 英数字のみのキーワード: 前後が半角英数字でないときだけ一致（単語境界）
    - 日本語などを含むキーワード: 部分一致
    - NFKC正規化（全角英数字を半角にそろえる）と大文字小文字の無視を行う
    """
    t = unicodedata.normalize("NFKC", str(text)).casefold()
    for k in keywords:
        k = unicodedata.normalize("NFKC", str(k)).strip().casefold()
        if not k:
            continue
        if _ASCII_KW.fullmatch(k):
            if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", t):
                return True
        elif k in t:
            return True
    return False


def pick(items, notified, now, window, top_n, prefix):
    fresh = [x for x in items
             if now - notified.get(f"{prefix}:{x['key']}", 0) >= window]
    fresh.sort(key=lambda x: (x["related"], x["score"]), reverse=True)
    return fresh[:top_n]


def main():
    log("start")
    dry_run = is_dry_run()
    if dry_run:
        log("DRY_RUN: Discordへ送信せず、state/*.json も保存しません")
        log("DRY_RUN selftest sanitize: "
            + sanitize("@everyone <@123> `x` **b** [a](https://evil.example) \u202e"))
        samples = ["mail", "AI news", "AI・ゲーム", "AIが進化", "OpenAI"]
        log("DRY_RUN selftest related(AI): "
            + " ".join(f"{s}={related(s, ['AI'])}" for s in samples))
    cfg = load_config()
    now = time.time()
    kw = cfg["keywords"]
    webhook = "" if dry_run else os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    # ---- 状態の読み込み ----
    baseline = state.load("baseline.json", {})
    if not isinstance(baseline, dict) or not isinstance(baseline.get("terms"), dict):
        baseline = {"runs": 0, "terms": {}}
    try:
        runs = int(baseline.get("runs", 0))
    except (TypeError, ValueError):
        runs = 0
    notified = state.load("notified.json", {})
    if not isinstance(notified, dict):
        notified = {}
    notified = {k: v for k, v in notified.items()
                if isinstance(v, (int, float)) and now - v < 7 * 86400}
    cache = state.load("translations.json", {})
    if not isinstance(cache, dict):
        cache = {}

    # ---- 1. 海外データ ----
    cands, jp, hn = [], set(), []
    try:
        cands = overseas.collect_overseas(cfg["overseas_geos"], cache)
    except Exception as e:
        log(f"WARN overseas failed: {type(e).__name__}")
    try:
        jp = overseas.get_jp_trends()
    except Exception as e:
        log(f"WARN jp trends failed: {type(e).__name__}")
    try:
        hn = overseas.get_hacker_news(cfg["hn_min_score"], cfg["hn_max_age_hours"])
    except Exception as e:
        log(f"WARN hacker news failed: {type(e).__name__}")
    log(f"overseas: candidates={len(cands)} jp_trends={len(jp)} hn={len(hn)}")

    # ---- 2. 日本の現状チェック（既に日本で出ている語・日本語名なしを除外）----
    seen, target_cands = set(), []
    for c in cands:
        ja = c.get("ja")
        if not ja or ja in seen:
            continue
        if c["term"].casefold() in jp or ja.casefold() in jp:
            continue
        seen.add(ja)
        target_cands.append(c)

    # ---- 3. Blueskyサンプリング ----
    res = None
    try:
        tagger = bluesky.make_tagger()
        res = bluesky.sample(int(cfg["collect_seconds"]),
                             [c["ja"] for c in target_cands], tagger)
    except Exception as e:
        log(f"WARN bluesky failed: {type(e).__name__}")
    posts = res["posts"] if res else 0
    sample_ok = posts >= int(cfg["min_sample_posts"])
    log(f"bluesky: posts={posts} terms={len(res['counts']) if res else 0} sample_ok={sample_ok}")

    # ---- 4. スコアリング ----
    landing, arriving, rising = [], [], []
    if sample_ok:
        for c in target_cands:
            m = res["target"].get(c["ja"], 0)
            item = {
                "key": c["ja"], "name": c["ja"], "geos": c["geos"], "mentions": m,
                "score": len(c["geos"]) * 2 + math.log10(max(c["traffic"], 1)),
                "related": related(c["ja"], kw),
            }
            (arriving if m >= int(cfg["landing_users"]) else landing).append(item)

        if runs >= int(cfg["warmup_runs"]):
            for r in bluesky.find_rising(res["counts"], posts, baseline["terms"],
                                         int(cfg["min_users"]), float(cfg["min_ratio"])):
                rising.append({"key": r["term"], "name": r["term"], "users": r["users"],
                               "ratio": r["ratio"], "score": r["score"],
                               "related": related(r["term"], kw)})
        else:
            log(f"warmup {runs + 1}/{cfg['warmup_runs']} (📈は出ません)")

        baseline["terms"] = bluesky.update_baseline(baseline["terms"], res["counts"], posts)
        runs += 1
    else:
        log("WARN Blueskyのサンプルが不足のため 🌊🔥📈 をスキップ（ベースラインも更新しません）")

    hn_items = [{"key": str(h["id"]), "name": h["title"], "id": h["id"], "score": h["score"],
                 "related": related(h["title"], kw)}
                for h in hn if isinstance(h.get("id"), int)]

    if cfg["only_related"]:
        landing = [x for x in landing if x["related"]]
        arriving = [x for x in arriving if x["related"]]
        rising = [x for x in rising if x["related"]]
        hn_items = [x for x in hn_items if x["related"]]

    # ---- 5. 重複排除 + 上位N件 ----
    window = float(cfg["renotify_hours"]) * 3600
    top_n = int(cfg["top_n"])
    p_landing = pick(landing, notified, now, window, top_n, "landing")
    p_arriving = pick(arriving, notified, now, window, top_n, "arriving")
    p_rising = pick(rising, notified, now, window, top_n, "rising")
    p_hn = pick(hn_items, notified, now, window, top_n, "hn")
    log(f"picked: landing={len(p_landing)} arriving={len(p_arriving)} "
        f"rising={len(p_rising)} hn={len(p_hn)}")

    # ---- 6. メッセージ組み立て（外部由来の文字列は必ず sanitize を通す）----
    sections = []
    if p_landing:
        lines = [f"- **{sanitize(x['name'])}** ｜ {','.join(x['geos'])} ｜ 日本 {x['mentions']}人"
                 for x in p_landing]
        sections.append("🌊 **上陸前**（海外で話題・日本は未反応）\n" + "\n".join(lines))
    if p_arriving:
        lines = [f"- **{sanitize(x['name'])}** ｜ {','.join(x['geos'])} ｜ 日本 {x['mentions']}人"
                 for x in p_arriving]
        sections.append("🔥 **上陸中**（日本で言及が増え始め）\n" + "\n".join(lines))
    if p_rising:
        lines = [f"- **{sanitize(x['name'])}** ｜ {x['users']}人 ｜ 平常比 x{x['ratio']:.1f}"
                 for x in p_rising]
        sections.append("📈 **Bluesky急上昇**\n" + "\n".join(lines))
    if p_hn:
        lines = [f"- [{sanitize(x['name'], 100)}](https://news.ycombinator.com/item?id={int(x['id'])})"
                 f" ｜ {x['score']}pt" for x in p_hn]
        sections.append("💻 **Hacker News**（参考）\n" + "\n".join(lines))

    if sections:
        jst = datetime.now(timezone(timedelta(hours=9))).strftime("%m/%d %H:%M JST")
        result = notify.send(webhook, "trend-radar", "\n\n".join(sections),
                             footer=jst, dry_run=dry_run)
        log(f"notify: {result}")
        if result == "sent":
            for prefix, group in (("landing", p_landing), ("arriving", p_arriving),
                                  ("rising", p_rising), ("hn", p_hn)):
                for x in group:
                    notified[f"{prefix}:{x['key']}"] = now
    else:
        log("notify: nothing to notify")

    # ---- 7. 状態の保存（DRY_RUN では保存しない）----
    if dry_run:
        log("DRY_RUN: state/*.json の保存をスキップしました")
    else:
        baseline["runs"] = runs
        state.save("baseline.json", baseline)
        state.save("notified.json", notified)
        if len(cache) > 5000:
            cache = dict(list(cache.items())[-5000:])
        state.save("translations.json", cache)

    if posts == 0 and not cands and not hn:
        log("ERROR 全データ源から取得できませんでした")
        sys.exit(1)
    log(f"done ({time.time() - now:.0f}s)")


if __name__ == "__main__":
    main()
