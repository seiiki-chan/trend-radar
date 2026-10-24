# src/notify.py
import re
import time
import unicodedata

import requests

from overseas import USER_AGENT

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MD_ESCAPE_RE = re.compile(r"([\\*_~|><\[\]()#:])")


def sanitize(text, limit=80):
    """外部由来の文字列を、Discordの本文に埋め込める安全な形にする。
    - NFKC正規化（全角の＠も@にそろえてから除去する）
    - URLは除去、制御文字・不可視文字は除去、@とバッククォートは除去
    - 長さを制限してから、Markdown記号をバックスラッシュでエスケープする
    """
    s = unicodedata.normalize("NFKC", str(text))
    s = _URL_RE.sub("URL省略", s)
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Zl", "Zp"):
            out.append(" ")
        elif cat in ("Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    s = "".join(out).replace("@", "").replace("`", "")
    s = re.sub(r"\s+", " ", s).strip()
    s = s[:limit].strip()
    if not s:
        return "（不明）"
    # 長さ制限のあとでエスケープする（途中で切れた \ が次の文字を打ち消すのを防ぐ）
    return _MD_ESCAPE_RE.sub(r"\\\1", s)


def _clip(description, limit=4000):
    """行の途中で切らないように、行単位で切り詰める。"""
    if len(description) <= limit:
        return description
    cut = description[:limit - 10]
    idx = cut.rfind("\n")
    if idx > 0:
        cut = cut[:idx]
    return cut + "\n…"


def send(webhook_url, title, description, footer="", color=0x1D9BF0, dry_run=False):
    """Discordへ送る。戻り値: 'sent' / 'printed'（未設定またはDRY_RUN）/ 'failed'"""
    description = _clip(description)
    if dry_run or not webhook_url:
        if dry_run:
            print("[radar] notify: DRY_RUN のためDiscordへは送信せず、ここに表示します", flush=True)
        else:
            print("[radar] notify: DISCORD_WEBHOOK_URL 未設定のため標準出力に表示します", flush=True)
        print(f"--- {title} ---\n{description}\n{footer}", flush=True)
        return "printed"

    payload = {
        "username": "trend-radar",
        "allowed_mentions": {"parse": []},  # 全メンション無効化
        "embeds": [{
            "title": title[:250],
            "description": description,
            "color": color,
            "footer": {"text": footer[:200]},
        }],
    }
    for attempt in range(3):
        try:
            r = requests.post(webhook_url, json=payload,
                              headers={"User-Agent": USER_AGENT}, timeout=15)
        except requests.RequestException as e:
            # 例外文字列にURLが含まれ得るので、型名だけを出す
            print(f"[radar] WARN discord post error: {type(e).__name__}", flush=True)
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (200, 204):
            return "sent"
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except (ValueError, AttributeError):
                wait = 2
            time.sleep(min(wait, 10))
            continue
        print(f"[radar] WARN discord HTTP {r.status_code}", flush=True)
        if r.status_code < 500:
            break
        time.sleep(2 * (attempt + 1))
    return "failed"
