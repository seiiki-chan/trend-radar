# src/state.py
import json
import os
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"


def load(name, default):
    """state/<name> を読む。無い・壊れている場合は default を返す。"""
    path = STATE_DIR / name
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save(name, data):
    """一時ファイルに書いてから置き換える（書き込み途中で壊れるのを防ぐ）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / name
    tmp = STATE_DIR / (name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, path)
