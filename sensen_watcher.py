#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Beyblade 上架 / 補貨偵測引擎（森森文具玩具批發 / Cyberbiz）。"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests


COLLECTION_API = os.environ.get(
    "SENSEN_COLLECTION_API",
    "https://www.sen-sen.com.tw/collections/%E6%88%B0%E9%AC%A5%E9%99%80%E8%9E%BAx.json",
)
SHOP_BASE = "https://www.sen-sen.com.tw"
SOURCE_NAME = "森森文具玩具"

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "sensen-beyblade-k7m2qz"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USERNAME = os.environ.get("DISCORD_USERNAME", "TheWatcher")
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "").strip()
DISCORD_ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "sensen_tracked_items.json")
FEED_FILE = os.environ.get("FEED_FILE", "sensen_feed.json")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "sensen_history.jsonl")
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "watchlist.json")
DEBUG = os.environ.get("DEBUG", "0") == "1"

RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.environ.get("RETRY_BASE_DELAY", "2"))
FLOOD_THRESHOLD = int(os.environ.get("FLOOD_THRESHOLD", "8"))
FAIL_ALERT_THRESHOLD = int(os.environ.get("FAIL_ALERT_THRESHOLD", "3"))
HISTORY_RETENTION_HOURS = int(os.environ.get("HISTORY_RETENTION_HOURS", "24"))
NOTIFY_PRICE_DROP = os.environ.get("NOTIFY_PRICE_DROP", "1") == "1"
META_KEY = "__meta__"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.sen-sen.com.tw/collections/%E6%88%B0%E9%AC%A5%E9%99%80%E8%9E%BAx",
}


def fetch_products():
    resp = requests.get(COLLECTION_API, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("森森分類 API 回傳格式不是商品陣列。")
    return data


def fetch_products_with_retry():
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fetch_products()
        except Exception as exc:
            last_error = exc
            if attempt < RETRY_ATTEMPTS:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                print(f"抓取失敗（第 {attempt}/{RETRY_ATTEMPTS} 次）：{exc}；{delay:.0f} 秒後重試…")
                time.sleep(delay)
    raise RuntimeError(str(last_error))


def parse_price(value):
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def detect_in_stock(raw):
    variants = raw.get("variants")
    if isinstance(variants, list) and variants:
        for variant in variants:
            qty = variant.get("inventory_quantity")
            if isinstance(qty, (int, float)) and qty > 0:
                return True
            if variant.get("inventory_policy") == "continue" and qty is not None:
                return True
        return False
    return True


def parse_product(raw):
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not title or not url:
        return None
    if not re.search(r"BEYBLADE|戰鬥陀螺|陀螺|UX-|BX-|CX-", title, re.I):
        return None

    full_url = urljoin(SHOP_BASE, url)
    key = url.rstrip("/").split("/")[-1] or title
    image = str(raw.get("photo") or "").strip()
    if image:
        image = urljoin(SHOP_BASE, image)

    return {
        "key": key,
        "title": title,
        "url": full_url,
        "price": parse_price(raw.get("price")),
        "in_stock": detect_in_stock(raw),
        "image": image,
    }


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(data):
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def split_meta(state):
    state = dict(state or {})
    meta = state.pop(META_KEY, {})
    return state, meta if isinstance(meta, dict) else {}


def save_state_with_meta(products, meta):
    out = dict(products)
    out[META_KEY] = meta
    save_state(out)


def load_watchlist():
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("keywords", [])
    if not isinstance(data, list):
        return []
    return [str(k).strip() for k in data if str(k).strip()]


def matched_keyword(title, keywords):
    t = (title or "").lower()
    for kw in keywords:
        if kw.lower() in t:
            return kw
    return None


def append_history(current, ts):
    rows = [
        json.dumps({
            "ts": ts,
            "id": p["key"],
            "title": p["title"],
            "price": p.get("price"),
            "in_stock": p.get("in_stock", True),
        }, ensure_ascii=False)
        for p in current.values()
    ]
    if rows:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
    prune_history(ts)


def parse_history_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def prune_history(now_ts):
    if HISTORY_RETENTION_HOURS <= 0 or not os.path.exists(HISTORY_FILE):
        return
    now_dt = parse_history_ts(now_ts) or datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(hours=HISTORY_RETENTION_HOURS)
    tmp = f"{HISTORY_FILE}.tmp"
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as src, open(tmp, "w", encoding="utf-8") as dst:
            for line in src:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row_ts = parse_history_ts(row.get("ts"))
                if row_ts and row_ts >= cutoff:
                    dst.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, HISTORY_FILE)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_feed(current, new_keys=(), restock_keys=()):
    new_keys, restock_keys = set(new_keys), set(restock_keys)
    products = []
    for key, p in current.items():
        status = "new" if key in new_keys else "restock" if key in restock_keys else "normal"
        products.append({
            "id": key,
            "title": p["title"],
            "url": p["url"],
            "price": p.get("price"),
            "in_stock": p.get("in_stock", True),
            "status": status,
            "first_seen": p.get("first_seen"),
            "image": p.get("image", ""),
        })
    products.sort(key=lambda x: ({"new": 0, "restock": 1, "normal": 2}[x["status"]], x["first_seen"] or ""))
    with open(FEED_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source": SOURCE_NAME,
            "count": len(products),
            "products": products,
        }, f, ensure_ascii=False, indent=2)


def ntfy_topics():
    return [topic.strip() for topic in str(NTFY_TOPIC).split(",") if topic.strip()]


def discord_webhook_urls():
    return [url.strip() for url in DISCORD_WEBHOOK_URL.split(",") if url.strip()]


def discord_mention_text():
    if DISCORD_ROLE_ID:
        role_ids = re.findall(r"\d+", DISCORD_ROLE_ID)
        return " ".join(f"<@&{role_id}>" for role_id in role_ids)
    return DISCORD_MENTION


def discord_allowed_mentions(mention):
    parse = []
    if "@everyone" in mention or "@here" in mention:
        parse.append("everyone")
    role_ids = list(dict.fromkeys(re.findall(r"<@&(\d+)>", mention)))
    allowed = {"parse": parse}
    if role_ids:
        allowed["roles"] = role_ids
    return allowed


def discord_publish(title, message, click=None):
    webhook_urls = discord_webhook_urls()
    if not webhook_urls:
        return
    mention = discord_mention_text()
    content = "\n".join(x for x in (mention, title, message) if x)
    if len(content) > 1900:
        content = content[:1897] + "..."
    embed = {
        "title": str(title)[:256],
        "description": str(message)[:4000],
        "color": 0x2F80ED,
    }
    if click:
        embed["url"] = click
    payload = {
        "username": DISCORD_USERNAME,
        "content": content,
        "embeds": [embed],
        "allowed_mentions": discord_allowed_mentions(mention),
    }
    for idx, webhook_url in enumerate(webhook_urls, start=1):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code >= 300:
                print(f"Discord webhook #{idx} 回應異常：{resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"發送 Discord webhook #{idx} 失敗：{e}")


def ntfy_publish(title, message, tags=None, priority=3, click=None):
    for topic in ntfy_topics():
        payload = {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags or [],
        }
        if click:
            payload["click"] = click
        try:
            resp = requests.post(NTFY_SERVER, data=json.dumps(payload).encode("utf-8"), timeout=10)
            if resp.status_code >= 300:
                print(f"ntfy topic {topic} 回應異常：{resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"發送 ntfy topic {topic} 通知失敗：{e}")
    discord_publish(title, message, click)


def price_text(p):
    return f"NT${int(p['price'])}" if p.get("price") else ""


def event_message(p, action, extra=""):
    stock = "有貨" if p.get("in_stock") else "目前缺貨／補貨中"
    lines = [p["title"]]
    detail = " ".join(x for x in (price_text(p), f"({stock})", extra) if x)
    if detail:
        lines.append(detail)
    lines.append("點我直接前往購買")
    return "\n".join(lines)


def send_starred(p, kind, kw):
    ntfy_publish(
        f"[{SOURCE_NAME}] 🔔 關注{kind}",
        event_message(p, kind, f"命中「{kw}」"),
        tags=["bell", "star"],
        priority=5,
        click=p["url"],
    )


def notify_delisted(delisted):
    for p in delisted:
        title = p.get("title", p.get("key", "（未知商品）"))
        ntfy_publish(
            f"[{SOURCE_NAME}] 🔻 已下架",
            f"{title}\n已從森森戰鬥陀螺清單消失。",
            tags=["arrow_down"],
            priority=2,
            click=p.get("url"),
        )


def summarize(kind, items):
    if not items:
        return []
    lines = [f"{kind} {len(items)} 項"]
    for p in items[:5]:
        lines.append(f"- {p['title']}")
    if len(items) > 5:
        lines.append(f"...還有 {len(items) - 5} 項")
    return lines


def send_notifications(new_items, restocks, price_drops, keywords=()):
    if keywords:
        starred_restocks = [(p, matched_keyword(p["title"], keywords)) for p in restocks]
        starred_news = [(p, matched_keyword(p["title"], keywords)) for p in new_items]
        for p, kw in starred_restocks:
            if kw:
                send_starred(p, "補貨", kw)
        for p, kw in starred_news:
            if kw:
                send_starred(p, "新上架", kw)
        restocks = [p for p, kw in starred_restocks if not kw]
        new_items = [p for p, kw in starred_news if not kw]

    total = len(new_items) + len(restocks) + len(price_drops)
    if not total:
        return
    if total > FLOOD_THRESHOLD:
        lines = []
        lines += summarize("🔁 補貨", restocks)
        lines += summarize("🆕 新上架", new_items)
        if price_drops:
            lines.append(f"📉 降價 {len(price_drops)} 項")
        ntfy_publish(f"[{SOURCE_NAME}] 陀螺大量異動", "\n".join(lines), tags=["bell"], priority=4)
        return

    for p in restocks:
        ntfy_publish(f"[{SOURCE_NAME}] 🔁 補貨", event_message(p, "補貨"), tags=["rotating_light"], priority=5, click=p["url"])
    for p in new_items:
        ntfy_publish(f"[{SOURCE_NAME}] 🆕 新上架", event_message(p, "新上架"), tags=["sparkles"], priority=4, click=p["url"])
    for p, old_price in price_drops:
        ntfy_publish(
            f"[{SOURCE_NAME}] 📉 降價",
            event_message(p, "降價", f"NT${int(old_price)} → NT${int(p['price'])}"),
            tags=["chart_with_downwards_trend"],
            priority=3,
            click=p["url"],
        )


def main():
    print("開始檢查 森森文具玩具 Beyblade 上架 / 補貨…")
    state, meta = split_meta(load_state())
    fail_count = int(meta.get("consecutive_failures", 0) or 0)

    try:
        raw = fetch_products_with_retry()
    except Exception as e:
        fail_count += 1
        meta["consecutive_failures"] = fail_count
        meta["last_failure_at"] = datetime.now(timezone.utc).isoformat()
        meta["last_error"] = str(e)
        save_state_with_meta(state, meta)
        print(f"抓取失敗：{e}（連續第 {fail_count} 次）")
        if fail_count >= FAIL_ALERT_THRESHOLD and (fail_count - FAIL_ALERT_THRESHOLD) % FAIL_ALERT_THRESHOLD == 0:
            ntfy_publish(
                f"[{SOURCE_NAME}] ⚠️ 連續抓取失敗",
                f"已連續 {fail_count} 次無法讀取森森戰鬥陀螺清單。\n最後錯誤：{e}",
                tags=["warning"],
                priority=4,
            )
        return

    if fail_count:
        print(f"抓取成功，連續失敗計數由 {fail_count} 歸零。")
    meta["consecutive_failures"] = 0
    meta.pop("last_error", None)
    meta.pop("last_failure_at", None)

    if DEBUG and raw:
        print(json.dumps(raw[0], ensure_ascii=False, indent=2))

    current = {}
    for prod in raw:
        p = parse_product(prod)
        if p:
            current[p["key"]] = p

    if not current:
        save_state_with_meta(state, meta)
        print("沒解析到任何森森 Beyblade 商品。")
        return

    now = datetime.now(timezone.utc).isoformat()
    append_history(current, now)

    if not state:
        for p in current.values():
            p["first_seen"] = now
        save_state_with_meta(current, meta)
        write_feed(current)
        print(f"首次執行：已記錄 {len(current)} 個商品為基準，下次有變動才通知。")
        return

    new_items, restocks, price_drops = [], [], []
    for key, p in current.items():
        old = state.get(key)
        if old is None:
            p["first_seen"] = now
            new_items.append(p)
        else:
            p["first_seen"] = old.get("first_seen", now)
            if old.get("in_stock") is False and p["in_stock"] is True:
                restocks.append(p)
            if NOTIFY_PRICE_DROP and old.get("price") and p.get("price") and p["price"] < old["price"]:
                price_drops.append((p, old["price"]))

    send_notifications(new_items, restocks, price_drops, load_watchlist())
    delisted = [old for key, old in state.items() if key not in current]
    notify_delisted(delisted)
    save_state_with_meta(current, meta)
    write_feed(current, new_keys=[p["key"] for p in new_items], restock_keys=[p["key"] for p in restocks])
    print(f"完成：新上架 {len(new_items)}、補貨 {len(restocks)}、降價 {len(price_drops)}、下架 {len(delisted)}。")


if __name__ == "__main__":
    main()
