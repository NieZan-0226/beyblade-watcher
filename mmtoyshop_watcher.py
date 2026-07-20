#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Beyblade 上架 / 補貨偵測引擎（M.M 小舖 / BVShop）。

MMToyShop 的分類頁商品是前端渲染，靜態 requests 抓不到商品卡；
因此這支 watcher 會優先使用 Playwright 讀取渲染後 DOM。
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


CATEGORY_URL = os.environ.get(
    "MMTOYSHOP_CATEGORY_URL",
    "https://mmtoyshop.com/category/%F0%9F%8C%80%E6%88%B0%E9%AC%A5%E9%99%80%E8%9E%BA",
)
SOURCE_NAME = "M.M小舖"

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "mmtoyshop-beyblade-k7m2qz"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USERNAME = os.environ.get("DISCORD_USERNAME", "TheWatcher")
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "mmtoyshop_tracked_items.json")
FEED_FILE = os.environ.get("FEED_FILE", "mmtoyshop_feed.json")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "mmtoyshop_history.jsonl")
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "watchlist.json")
DEBUG = os.environ.get("DEBUG", "0") == "1"

FLOOD_THRESHOLD = int(os.environ.get("FLOOD_THRESHOLD", "8"))
FAIL_ALERT_THRESHOLD = int(os.environ.get("FAIL_ALERT_THRESHOLD", "3"))
HISTORY_RETENTION_HOURS = int(os.environ.get("HISTORY_RETENTION_HOURS", "24"))
NOTIFY_PRICE_DROP = os.environ.get("NOTIFY_PRICE_DROP", "1") == "1"
META_KEY = "__meta__"


def fetch_products():
    """用 Playwright 開啟分類頁，擷取渲染後商品卡。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "MMToyShop 需要 Playwright 才能讀取前端渲染商品；"
            "請安裝 playwright 並執行 `python -m playwright install chromium`。"
        ) from exc

    products = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        )
        page.goto(CATEGORY_URL, wait_until="load", timeout=60000)
        page.wait_for_selector(".productBoxStyle", timeout=45000)
        page.wait_for_timeout(1500)
        products = page.eval_on_selector_all(
            ".productBoxStyle",
            """cards => cards.map(card => {
                const titleEl = card.querySelector('.productBoxTitle');
                const priceEl = card.querySelector('.productBoxPrice');
                const soldoutEl = card.querySelector('.productBoxSoldout, .soldout-hint');
                const stockText = card.innerText || '';
                const linkEl = card.querySelector('a[href*="/item/"]');
                const imgEl = card.querySelector('img');
                return {
                    title: titleEl ? titleEl.innerText.trim() : '',
                    price_text: priceEl ? priceEl.innerText.trim() : '',
                    soldout_text: soldoutEl ? soldoutEl.innerText.trim() : '',
                    text: stockText,
                    url: linkEl ? linkEl.href : '',
                    image: imgEl ? (imgEl.currentSrc || imgEl.src || '') : '',
                };
            })"""
        )
        browser.close()
    return products


def parse_price(text):
    if not text:
        return None
    m = re.search(r"([\d,]+)", str(text))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_product(raw):
    title = (raw.get("title") or "").strip()
    url = (raw.get("url") or "").strip()
    if not title or not url:
        return None

    key = url.rstrip("/").split("/")[-1] or title
    text = " ".join(str(raw.get(k, "")) for k in ("soldout_text", "text"))
    in_stock = not bool(re.search(r"補貨中|售完|已售完|庫存\s*0|缺貨", text))

    return {
        "key": key,
        "title": title,
        "url": url,
        "price": parse_price(raw.get("price_text")),
        "in_stock": in_stock,
        "image": raw.get("image") or "",
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


def discord_publish(title, message, click=None):
    webhook_urls = discord_webhook_urls()
    if not webhook_urls:
        return
    content = "\n".join(x for x in (DISCORD_MENTION, title, message) if x)
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
        "allowed_mentions": {"parse": ["everyone"] if DISCORD_MENTION else []},
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
            f"{title}\n已從 M.M小舖戰鬥陀螺清單消失。",
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
    print("開始檢查 M.M小舖 Beyblade 上架 / 補貨…")
    state, meta = split_meta(load_state())
    fail_count = int(meta.get("consecutive_failures", 0) or 0)

    try:
        raw = fetch_products()
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
                f"已連續 {fail_count} 次無法讀取 M.M小舖商品頁。\n最後錯誤：{e}",
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
        print("沒解析到任何 M.M小舖商品。")
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
