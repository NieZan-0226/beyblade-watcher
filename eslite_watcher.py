#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Beyblade 上架 / 補貨偵測引擎（誠品線上精選策展頁）。

誠品頁面會經過 Cloudflare 與前端渲染；一般 requests 很容易被擋。
因此這支 watcher 使用 Playwright 開啟策展頁，擷取頁面上的商品連結。
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests


EXHIBITION_URL = os.environ.get(
    "ESLITE_EXHIBITION_URL",
    "https://www.eslite.com/exhibitions/CU202503-00091",
)
SOURCE_NAME = "誠品線上"

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "eslite-beyblade-k7m2qz"
STATE_FILE = os.environ.get("STATE_FILE", "eslite_tracked_items.json")
FEED_FILE = os.environ.get("FEED_FILE", "eslite_feed.json")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "eslite_history.jsonl")
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "watchlist.json")
DEBUG = os.environ.get("DEBUG", "0") == "1"

FLOOD_THRESHOLD = int(os.environ.get("FLOOD_THRESHOLD", "8"))
FAIL_ALERT_THRESHOLD = int(os.environ.get("FAIL_ALERT_THRESHOLD", "3"))
HISTORY_RETENTION_HOURS = int(os.environ.get("HISTORY_RETENTION_HOURS", "24"))
NOTIFY_PRICE_DROP = os.environ.get("NOTIFY_PRICE_DROP", "1") == "1"
META_KEY = "__meta__"


def fetch_products():
    """用 Playwright 開啟誠品策展頁，擷取渲染後商品連結。"""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "誠品線上需要 Playwright 才能讀取前端渲染頁；"
            "請安裝 playwright 並執行 `python -m playwright install chromium`。"
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            locale="zh-TW",
            viewport={"width": 1365, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        try:
            page.goto(EXHIBITION_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            body_text = page.locator("body").inner_text(timeout=10000)
            if re.search(r"Sorry, you have been blocked|Attention Required|Cloudflare", body_text, re.I):
                raise RuntimeError("誠品線上回傳 Cloudflare 封鎖頁，暫時無法解析。")

            # 部分商品卡會 lazy load；慢慢捲到底讓商品區塊都載入。
            for _ in range(8):
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(600)

            try:
                page.wait_for_selector('a[href*="/product/"]', timeout=15000)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("誠品策展頁沒有找到商品連結，可能版型改變或被導向。") from exc

            products = page.eval_on_selector_all(
                'a[href*="/product/"]',
                """anchors => {
                    const rows = [];
                    const seen = new Set();
                    for (const a of anchors) {
                        const href = a.href || '';
                        if (!href || seen.has(href)) continue;
                        seen.add(href);

                        let card = a;
                        for (let i = 0; i < 6 && card.parentElement; i++) {
                            card = card.parentElement;
                            const txt = (card.innerText || '').trim();
                            if (txt.length > 30 && txt.length < 1200) break;
                        }

                        const img = a.querySelector('img') || card.querySelector('img');
                        const text = (card.innerText || a.innerText || '').trim();
                        const title =
                            (a.innerText || '').trim() ||
                            a.getAttribute('aria-label') ||
                            a.getAttribute('title') ||
                            (img ? img.getAttribute('alt') : '') ||
                            '';
                        rows.push({
                            title,
                            url: href,
                            text,
                            image: img ? (img.currentSrc || img.src || '') : '',
                        });
                    }
                    return rows;
                }""",
            )
            return products
        finally:
            browser.close()


def parse_price(text):
    if not text:
        return None
    patterns = [
        r"(?:NT\$|NTD|\$)\s*([\d,]+)",
        r"售價\s*([\d,]+)",
        r"優惠價\s*([\d,]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, str(text), re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                return None
    return None


def clean_title(title):
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    title = re.sub(r"^商品\s*", "", title)
    return title


def parse_product(raw):
    text = str(raw.get("text") or "")
    fallback_title = text.splitlines()[0] if text else ""
    title = clean_title(raw.get("title") or fallback_title)
    url = str(raw.get("url") or "").strip()
    if not title or not url:
        return None

    combined = f"{title}\n{text}"
    if not re.search(r"BEYBLADE|戰鬥陀螺|陀螺|UX-|BX-|CX-", combined, re.I):
        return None

    m = re.search(r"/product/([^/?#]+)", url)
    key = m.group(1) if m else url.rstrip("/").split("/")[-1]
    in_stock = not bool(re.search(r"已售完|售完|缺貨|補貨中|暫無庫存|已下架", combined))

    return {
        "key": key,
        "title": title,
        "url": url,
        "price": parse_price(combined),
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


def ntfy_publish(title, message, tags=None, priority=3, click=None):
    payload = {
        "topic": NTFY_TOPIC,
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
            print(f"ntfy 回應異常：{resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"發送 ntfy 通知失敗：{e}")


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
            f"{title}\n已從誠品線上 BEYBLADE 策展頁消失。",
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
    print("開始檢查 誠品線上 Beyblade 策展頁…")
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
                f"已連續 {fail_count} 次無法讀取誠品線上策展頁。\n最後錯誤：{e}",
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
        print(json.dumps(raw[:3], ensure_ascii=False, indent=2))

    current = {}
    for prod in raw:
        p = parse_product(prod)
        if p:
            current[p["key"]] = p

    if not current:
        save_state_with_meta(state, meta)
        print("沒解析到任何誠品線上 Beyblade 商品。")
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
