#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次執行 Funbox、Toys"R"Us、誠品線上、森森、孩子玩伴與 MOMO Beyblade 監控器，並合併看板資料。"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TOPIC = "the-watcher-beyblade-k7m2qz"

WATCHERS = (
    {
        "key": "funbox",
        "name": "Funbox",
        "script": "funbox_watcher.py",
        "state": "funbox_tracked_items.json",
        "feed": "funbox_feed.json",
        "history": "funbox_history.jsonl",
        "topic_env": "FUNBOX_NTFY_TOPIC",
    },
    {
        "key": "toysrus",
        "name": "玩具反斗城",
        "script": "toysrus_watcher.py",
        "state": "toysrus_tracked_items.json",
        "feed": "toysrus_feed.json",
        "history": "toysrus_history.jsonl",
        "topic_env": "TOYSRUS_NTFY_TOPIC",
    },
    {
        "key": "eslite",
        "name": "誠品線上",
        "script": "eslite_watcher.py",
        "state": "eslite_tracked_items.json",
        "feed": "eslite_feed.json",
        "history": "eslite_history.jsonl",
        "topic_env": "ESLITE_NTFY_TOPIC",
    },
    {
        "key": "sensen",
        "name": "森森文具玩具",
        "script": "sensen_watcher.py",
        "state": "sensen_tracked_items.json",
        "feed": "sensen_feed.json",
        "history": "sensen_history.jsonl",
        "topic_env": "SENSEN_NTFY_TOPIC",
    },
    {
        "key": "kidplaymate",
        "name": "孩子玩伴",
        "script": "kidplaymate_watcher.py",
        "state": "kidplaymate_tracked_items.json",
        "feed": "kidplaymate_feed.json",
        "history": "kidplaymate_history.jsonl",
        "topic_env": "KIDPLAYMATE_NTFY_TOPIC",
    },
    {
        "key": "momo",
        "name": "MOMO 墊腳石",
        "script": "momo_watcher.py",
        "state": "momo_tracked_items.json",
        "feed": "momo_feed.json",
        "history": "momo_history.jsonl",
        "topic_env": "MOMO_NTFY_TOPIC",
    },
    {
        "key": "momo_funbox",
        "name": "MOMO Funbox",
        "script": "momo_funbox_watcher.py",
        "state": "momo_funbox_tracked_items.json",
        "feed": "momo_funbox_feed.json",
        "history": "momo_funbox_history.jsonl",
        "topic_env": "MOMO_FUNBOX_NTFY_TOPIC",
    },
)


def run_watcher(config):
    """以獨立狀態檔執行單一來源；其中一個失敗不會阻斷另一個。"""
    env = os.environ.copy()
    common_topic = env.get("NTFY_TOPIC") or DEFAULT_TOPIC
    env["NTFY_TOPIC"] = env.get(config["topic_env"]) or common_topic
    env["STATE_FILE"] = config["state"]
    env["FEED_FILE"] = config["feed"]
    env["HISTORY_FILE"] = config["history"]
    env["WATCHLIST_FILE"] = "watchlist.json"

    print(f"\n===== 檢查 {config['name']} =====", flush=True)
    result = subprocess.run(
        [sys.executable, config["script"]],
        cwd=BASE_DIR,
        env=env,
        check=False,
    )
    if result.returncode:
        print(f"{config['name']} 監控器異常結束（code={result.returncode}）。")
    return result.returncode


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def merge_feeds():
    products = []
    for config in WATCHERS:
        feed = read_json(BASE_DIR / config["feed"], {"products": []})
        for raw in feed.get("products", []):
            product = dict(raw)
            original_id = str(product.get("id", product.get("title", "")))
            product["id"] = f"{config['key']}:{original_id}"
            product["source_id"] = original_id
            product["source"] = config["name"]
            products.append(product)

    status_order = {"new": 0, "restock": 1, "normal": 2}
    products.sort(key=lambda p: (
        status_order.get(p.get("status"), 2),
        p.get("first_seen") or "",
        p.get("title") or "",
    ))
    combined = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Funbox + Toys\"R\"Us Taiwan + 誠品線上 + 森森文具玩具 + 孩子玩伴 + MOMO 墊腳石 + MOMO Funbox",
        "count": len(products),
        "products": products,
    }
    write_json(BASE_DIR / "feed.json", combined)
    return len(products)


def merge_histories():
    rows = []
    for config in WATCHERS:
        path = BASE_DIR / config["history"]
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            original_id = str(row.get("id", ""))
            row["id"] = f"{config['key']}:{original_id}"
            row["source_id"] = original_id
            row["source"] = config["name"]
            rows.append(row)
    rows.sort(key=lambda row: (str(row.get("ts", "")), str(row.get("id", ""))))
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    (BASE_DIR / "history.jsonl").write_text(content, encoding="utf-8")


def main():
    merge_only = "--merge-only" in sys.argv
    return_codes = []
    if not merge_only:
        return_codes = [run_watcher(config) for config in WATCHERS]

    count = merge_feeds()
    merge_histories()
    print(f"\n===== 整合完成：看板共 {count} 項商品 =====")
    if any(return_codes):
        print("至少一個來源執行異常；已保留其舊資料，另一個來源仍正常處理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
