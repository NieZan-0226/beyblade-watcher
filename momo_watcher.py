#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Beyblade 上架 / 補貨偵測引擎（MOMO 墊腳石 / 3PF）。"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urljoin

import requests
import urllib3


SHOP_BASE = "https://www.momoshop.com.tw"
API_URL = os.environ.get(
    "MOMO_API_URL",
    "https://3pf.momo.com.tw/shop/app/category/goods/query/v1",
)
SHOP_URL = os.environ.get(
    "MOMO_SHOP_URL",
    "https://www.momoshop.com.tw/TP/TP0002451/main?brand=TAKARA%20TOMY&brandNo=20190717104950516&curPage=1",
)
ENTP_CODE = os.environ.get("MOMO_ENTP_CODE", "TP0002451")
BRAND_NAME = os.environ.get("MOMO_BRAND_NAME", "TAKARA TOMY")
BRAND_NO = os.environ.get("MOMO_BRAND_NO", "20190717104950516")
CATE_CODE = os.environ.get("MOMO_CATE_CODE", "430000000000")
CATE_LEVEL = int(os.environ.get("MOMO_CATE_LEVEL", "1"))
KEYWORD_RE = os.environ.get("MOMO_KEYWORD_RE", r"BEYBLADE")
MAX_PAGES = int(os.environ.get("MOMO_MAX_PAGES", "30"))
FETCH_MODE = os.environ.get("MOMO_FETCH_MODE", "rendered").strip().lower()
RENDER_TIMEOUT_MS = int(os.environ.get("MOMO_RENDER_TIMEOUT_MS", "60000"))
VERIFY_SSL = os.environ.get("MOMO_VERIFY_SSL", "0") == "1"
SOURCE_NAME = os.environ.get("MOMO_SOURCE_NAME", "MOMO 墊腳石")

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "momo-beyblade-k7m2qz"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USERNAME = os.environ.get("DISCORD_USERNAME", "TheWatcher")
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "").strip()
DISCORD_ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "momo_tracked_items.json")
FEED_FILE = os.environ.get("FEED_FILE", "momo_feed.json")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "momo_history.jsonl")
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "watchlist.json")
PRODUCT_IDS_FILE = os.environ.get("MOMO_PRODUCT_IDS_FILE", "").strip()
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
    "Content-Type": "application/json",
    "Origin": SHOP_BASE,
    "Referer": SHOP_URL,
    "rc": "",
    "version": "6.11.0",
}

if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def fetch_products_page(page=1):
    payload = {
        "host": "momoshop",
        "data": {
            "entpCode": ENTP_CODE,
            "curPage": page,
            "cateCode": CATE_CODE,
            "cateLevel": CATE_LEVEL,
            "brand": BRAND_NAME,
            "brandNo": BRAND_NO,
            "_brandNameList": BRAND_NAME,
            "_brandNoList": BRAND_NO,
        },
    }
    resp = requests.post(
        API_URL,
        headers=HEADERS,
        data=json.dumps(payload).encode("utf-8"),
        timeout=25,
        verify=VERIFY_SSL,
    )
    resp.raise_for_status()
    data = json.loads(resp.content.decode("utf-8"))
    if not data.get("success"):
        raise RuntimeError(f"MOMO API 回傳失敗：{data.get('resultCode')} {data.get('resultMessage')}")
    goods_data = data.get("rtnGoodsData") or data.get("rtnSearchData") or {}
    products = goods_data.get("goodsInfoList")
    if not isinstance(products, list):
        raise RuntimeError("MOMO API 回傳格式不是商品清單。")
    return data, products


def fetch_products():
    if FETCH_MODE in {"product_ids", "product-id", "ids", "direct"}:
        return fetch_products_from_product_ids()

    if FETCH_MODE in {"rendered", "playwright", "page"}:
        page_error = None
        page_products = []
        try:
            page_products = fetch_products_from_rendered_page()
        except Exception as exc:
            page_error = exc
        products = merge_raw_products(page_products, fetch_products_from_product_ids())
        if products:
            if page_error:
                print(f"{SOURCE_NAME} 分類頁抓取失敗，已改用商品 ID 清單繼續監控：{page_error}")
            return products
        if page_error:
            raise page_error
        return products

    first, products = fetch_products_page(1)
    total_pages = int(first.get("maxPage") or first.get("totalPage") or 1)
    page_limit = min(total_pages, MAX_PAGES)
    for page in range(2, page_limit + 1):
        _, page_products = fetch_products_page(page)
        products.extend(page_products)
        time.sleep(0.12)
    return merge_raw_products(products, fetch_products_from_product_ids())


def raw_product_key(raw):
    if not isinstance(raw, dict):
        return ""
    value = str(raw.get("goodsCode") or raw.get("key") or raw.get("id") or "").strip()
    if value:
        return value
    url = str(raw.get("goodsUrl") or raw.get("url") or "").strip()
    match = re.search(r"(TP\d{13,})", url) or re.search(r"[?&]i_code=(\d+)", url) or re.search(r"/product/(\d+)", url)
    return match.group(1) if match else ""


def merge_raw_products(*groups):
    merged = {}
    for group in groups:
        for raw in group or []:
            key = raw_product_key(raw)
            if key:
                merged[key] = raw
    return list(merged.values())


def load_product_ids():
    if not PRODUCT_IDS_FILE:
        return []
    try:
        with open(PRODUCT_IDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"無法讀取 MOMO 商品 ID 清單 {PRODUCT_IDS_FILE}：{exc}") from exc
    if isinstance(data, dict):
        data = data.get("ids") or data.get("product_ids") or []
    if not isinstance(data, list):
        raise RuntimeError(f"MOMO 商品 ID 清單 {PRODUCT_IDS_FILE} 格式需為陣列或含 ids 的物件。")
    ids = []
    for item in data:
        match = re.search(r"(\d{6,})", str(item))
        if match:
            ids.append(match.group(1))
    return list(dict.fromkeys(ids))


def fetch_products_from_product_ids():
    ids = load_product_ids()
    if not ids:
        return []
    products = []
    for product_id in ids:
        try:
            products.append(fetch_product_page(product_id))
            time.sleep(0.15)
        except Exception as exc:
            print(f"MOMO 商品頁 {product_id} 抓取失敗：{exc}")
    return products


def fetch_product_page(product_id):
    url = f"{SHOP_BASE}/product/{product_id}"
    headers = dict(HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    headers["Referer"] = SHOP_URL
    resp = requests.get(url, headers=headers, timeout=25, verify=VERIFY_SSL)
    resp.raise_for_status()
    html = resp.content.decode("utf-8", errors="replace")
    return parse_product_page_html(product_id, html, url)


def html_meta(html, name):
    pattern = rf'<meta\s+(?:name|property)=["\']{re.escape(name)}["\']\s+content=["\']([^"\']*)["\']'
    match = re.search(pattern, html, re.I)
    return unescape(match.group(1)).strip() if match else ""


def html_json_string(html, key):
    for source in (html, html.replace(r"\"", '"').replace(r"\/", "/")):
        match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', source)
        if not match:
            continue
        try:
            return json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            return unescape(match.group(1)).strip()
    return ""


def parse_product_page_html(product_id, html, url):
    title = html_json_string(html, "goodsName") or html_meta(html, "og:title") or html_meta(html, "twitter:title")
    title = re.sub(r"\s+-\s*momo購物網.*$", "", title).strip()
    price = html_meta(html, "product:price:amount")
    image = html_meta(html, "og:image") or html_meta(html, "twitter:image")
    stock = html_json_string(html, "goodsStock")
    payment = html_json_string(html, "goodsPaymentDescription")
    if not payment:
        match = re.search(r"\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}\s*開賣|可訂購時通知我|售完補貨中|補貨中|售完", html)
        payment = match.group(0).strip() if match else ""
    can_tip_stock = html_json_string(html, "canTipStock")
    text = " ".join(x for x in (title, payment, stock, can_tip_stock, html[:8000]) if x)
    in_stock = bool(stock and stock != "0") and not re.search(r"售完|補貨中|缺貨|熱銷一空|可訂購時通知我|開賣", payment)
    return {
        "key": product_id,
        "title": title,
        "url": url,
        "image": image,
        "priceText": price,
        "text": text,
        "availability_text": payment,
        "in_stock": in_stock,
    }


def fetch_products_from_rendered_page():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            f"{SOURCE_NAME} 需要 Playwright 才能讀取前端渲染後的 TAKARA TOMY 商品；"
            "請先安裝 playwright 並執行 `python3 -m playwright install chromium`。"
        ) from exc

    js = """
    () => {
      const anchors = Array.from(document.querySelectorAll(
        '.product-item-goods a[href*="goodsDetail"], a[href*="GoodsDetail.jsp?i_code="], a[href*="/goods/GoodsDetail.jsp"]'
      ));
      const seen = new Set();
      const items = [];
      const findCard = (link) => {
        const storeCard = link.closest('.product-item-goods');
        if (storeCard) return storeCard;
        let el = link.parentElement;
        while (el && el !== document.body) {
          const cls = String(el.className || '');
          if (el.getAttribute('title') || cls.includes('mu-min-h') || cls.includes('hover:mu-shadow')) return el;
          el = el.parentElement;
        }
        return link.parentElement;
      };
      for (const link of anchors) {
        const url = link.href || '';
        if (!url || seen.has(url)) continue;
        const card = findCard(link);
        const img = (card && card.querySelector('img[alt*="TAKARA"], img[alt*="BEYBLADE"], img[alt], .fbm-thumbnail-img'))
          || link.querySelector('img[alt]');
        const text = ((card && card.innerText) || link.innerText || '').trim();
        const availabilityText = (
          (text.match(/\\d{2}\\/\\d{2}\\s+\\d{2}:\\d{2}\\s*開賣/) || [])[0]
          || (text.match(/可訂購時通知我/) || [])[0]
          || (text.match(/售完補貨中/) || [])[0]
          || (text.match(/熱銷一空/) || [])[0]
          || (text.match(/僅剩\\s*\\d+\\s*(?:組|件|個)?/) || [])[0]
          || ''
        );
        const title = (
          link.getAttribute('title')
          || (card && card.getAttribute('title'))
          || (link.innerText || '').trim()
          || (img && img.getAttribute('alt'))
          || ''
        ).trim();
        if (!/TAKARA\\s*TOMY/i.test(title || text)) continue;
        seen.add(url);
        items.push({
          title,
          url,
          image: img ? img.src : '',
          priceText: ((text.match(/\\$\\s*[\\d,]+(?:起)?/) || [])[0] || ''),
          text,
          availability_text: availabilityText,
          in_stock: !/(售完|補貨中|缺貨|熱銷一空|可訂購時通知我|開賣)/.test(text)
        });
      }
      return items;
    }
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            page = browser.new_page(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1600, "height": 1200},
                locale="zh-TW",
            )
            page.goto(SHOP_URL, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
            try:
                page.wait_for_selector(
                    '.product-item-goods, a[href*="GoodsDetail.jsp?i_code="], a[href*="/goods/GoodsDetail.jsp"]',
                    timeout=RENDER_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("MOMO 頁面載入完成，但找不到商品卡片。") from exc
            page.wait_for_timeout(1500)
            products = page.evaluate(js)
        finally:
            browser.close()

    if not isinstance(products, list):
        raise RuntimeError("MOMO 渲染頁面回傳格式不是商品清單。")
    return products


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
        text = str(value).replace(",", "").replace("$", "").strip()
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            return None
        return float(match.group(0))
    except (TypeError, ValueError):
        return None


def detect_in_stock(raw):
    if isinstance(raw.get("in_stock"), bool):
        return raw["in_stock"]
    text = str(raw.get("text") or raw.get("goodsStatus") or raw.get("goodsIconType") or "").strip()
    if re.search(r"售完|補貨中|缺貨|熱銷一空|可訂購時通知我|開賣", text):
        return False
    if re.search(r"僅剩|現貨|加入購物車", text):
        return True
    stock = raw.get("goodsStock")
    try:
        return int(str(stock).replace(",", "").strip()) > 0
    except (TypeError, ValueError):
        return text not in {"熱銷一空", "售完", "補貨中"}


def parse_product(raw):
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("goodsName") or raw.get("title") or "").strip()
    if not title or not re.search(KEYWORD_RE, title, re.I):
        return None
    url = str(raw.get("goodsUrl") or raw.get("url") or "").strip()
    goods_code = str(raw.get("goodsCode") or raw.get("key") or "").strip()
    if not goods_code and url:
        match = re.search(r"(TP\d{13,})", url) or re.search(r"[?&]i_code=(\d+)", url) or re.search(r"/product/(\d+)", url)
        if match:
            goods_code = match.group(1)
    if not goods_code:
        return None
    if not url:
        url = f"{SHOP_BASE}/TP/{ENTP_CODE}/goodsDetail/{goods_code}"
    if url.startswith("/"):
        url = urljoin(SHOP_BASE, url)

    return {
        "key": goods_code,
        "title": title,
        "url": url,
        "price": parse_price(raw.get("goodsPrice") or raw.get("SALE_PRICE") or raw.get("price") or raw.get("priceText")),
        "in_stock": detect_in_stock(raw),
        "image": str(raw.get("imgUrl") or raw.get("image") or "").strip(),
        "availability_text": extract_availability_text(raw),
    }


def extract_availability_text(raw):
    text = str(
        raw.get("availability_text")
        or raw.get("availabilityText")
        or raw.get("goodsStatus")
        or raw.get("text")
        or ""
    ).strip()
    patterns = (
        r"\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}\s*開賣",
        r"可訂購時通知我",
        r"售完補貨中",
        r"熱銷一空",
        r"僅剩\s*\d+\s*(?:組|件|個)?",
        r"補貨中",
        r"缺貨",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0).strip()
    return ""


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
            "availability_text": p.get("availability_text", ""),
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
            "availability_text": p.get("availability_text", ""),
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
    availability = p.get("availability_text") or ""
    stock = availability or ("有貨" if p.get("in_stock") else "目前缺貨／補貨中")
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
            f"{title}\n已從 {SOURCE_NAME} BEYBLADE 清單消失。",
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
    print(f"開始檢查 {SOURCE_NAME} BEYBLADE 上架 / 補貨…")
    state, meta = split_meta(load_state())
    fail_count = int(meta.get("consecutive_failures", 0) or 0)
    initialized = bool(meta.get("initialized"))

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
                f"已連續 {fail_count} 次無法讀取 {SOURCE_NAME} BEYBLADE 清單。\n最後錯誤：{e}",
                tags=["warning"],
                priority=4,
            )
        return

    if fail_count:
        print(f"抓取成功，連續失敗計數由 {fail_count} 歸零。")
    meta["consecutive_failures"] = 0
    meta["initialized"] = True
    meta["max_pages"] = MAX_PAGES
    meta["keyword_re"] = KEYWORD_RE
    meta["fetch_mode"] = FETCH_MODE
    meta["source_count"] = len(raw)
    meta.pop("last_error", None)
    meta.pop("last_failure_at", None)

    if DEBUG and raw:
        print(json.dumps(raw[0], ensure_ascii=False, indent=2))

    current = {}
    for prod in raw:
        p = parse_product(prod)
        if p:
            current[p["key"]] = p

    now = datetime.now(timezone.utc).isoformat()
    append_history(current, now)

    if not initialized:
        for p in current.values():
            p["first_seen"] = now
        save_state_with_meta(current, meta)
        write_feed(current)
        print(f"首次執行：已記錄 {len(current)} 個 {SOURCE_NAME} BEYBLADE 商品為基準，下次有變動才通知。")
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
