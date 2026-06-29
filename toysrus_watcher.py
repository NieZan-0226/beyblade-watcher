#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Beyblade 上架 / 補貨偵測引擎（Toys\"R\"Us Taiwan）

功能：
  - 新上架：清單出現沒看過的商品
  - 補貨  ：原本「缺貨」的商品變成「有貨」
  - 降價  ：價格比上次記錄低（可關閉）
推播：ntfy.sh（手機裝 ntfy app 訂閱一個 topic 即可收到，免費、免帳號）

執行：
  NTFY_TOPIC=你的隨機topic python3 toysrus_watcher.py
第一次跑只會建立基準清單、不發通知（避免洗版）。

核對真實 JSON 欄位（強烈建議第一次先做）：
  DEBUG=1 NTFY_TOPIC=test python3 toysrus_watcher.py
  會印出第一筆商品的原始結構，照著調整下面的 FIELD 設定。
"""

import html
import os
import re
import time
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests

# ============ 設定區（都可用環境變數覆蓋，密碼絕不要寫死在程式裡）============
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
# ⚠️ 換成你自己的隨機字串（越亂越好，等於密碼，知道的人都能看到你的通知）
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "toysrus-beyblade-k7m2qz"

CATEGORY_URL = "https://www.toysrus.com.tw/zh-tw/beyblade/"
SHOP_BASE = "https://www.toysrus.com.tw"
STATE_FILE = os.environ.get("STATE_FILE", "tracked_items.json")
FEED_FILE = os.environ.get("FEED_FILE", "feed.json")  # 給 iOS app 讀的清單
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "watchlist.json")  # 關注清單關鍵字
HISTORY_FILE = os.environ.get("HISTORY_FILE", "history.jsonl")  # 歷史紀錄（append 累積）
HISTORY_RETENTION_HOURS = int(os.environ.get("HISTORY_RETENTION_HOURS", "24"))  # history 只保留最近 N 小時

PAGE_SIZE = 48           # 網站自身宣告的每頁商品數
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DOWNLOAD_DEADLINE = 30
NOTIFY_PRICE_DROP = os.environ.get("NOTIFY_PRICE_DROP", "1") == "1"
FLOOD_THRESHOLD = 15     # 單次事件超過這個數，改發一則摘要避免洗版
DEBUG = os.environ.get("DEBUG", "0") == "1"

# 抓取重試：連不到 Toys\"R\"Us 時，先重試幾次再算失敗。
RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "3"))      # 總共嘗試幾次
RETRY_BASE_DELAY = float(os.environ.get("RETRY_BASE_DELAY", "2"))  # 退避基準秒數（指數成長）
# 連續失敗達到這個次數才發警告，偶爾一次逾時不吵人。成功一次就歸零。
FAIL_ALERT_THRESHOLD = int(os.environ.get("FAIL_ALERT_THRESHOLD", "4"))
# 狀態檔裡存放監看器自身狀態（連續失敗次數等）的保留鍵，不會和商品鍵衝突。
META_KEY = "__watcher_meta__"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
}

TITLE_FIELDS = ("title", "name")
ID_FIELDS = ("id", "product_id", "sku", "handle")
URL_FIELDS = ("url", "link", "handle")
PRICE_FIELDS = ("price", "sale_price", "price_min", "min_price", "lowest_price")

PRODUCT_BLOCK_RE = re.compile(
    r'<div class="card product-tile product-data product"(?P<attrs>.*?)>'
    r'(?P<body>.*?)<!-- END_dwmarker -->',
    re.DOTALL,
)


# ============ 抓取 ============
def fetch_all_products():
    """抓取品牌頁 HTML，回傳統一格式的商品 dict 清單。"""
    page_html = download_html(CATEGORY_URL)
    products = parse_products_html(page_html)
    if not products:
        raise RuntimeError("商品頁可連線，但解析不到任何商品")

    # 頁面會顯示「已顯示 / 總數」。若解析數少於總數，視為失敗，
    # 避免網頁改版或不完整回應被誤判為「大量下架」。
    count_match = re.search(r'(\d+)\s*/\s*(\d+)\s*產品', page_html)
    if count_match:
        expected = int(count_match.group(2))
        start = len(products)
        while start < expected:
            page_url = (
                f"{SHOP_BASE}/on/demandware.store/Sites-ToysRUs_TW-Site/zh_TW/"
                f"Search-UpdateGrid?cgid=beyblade&start={start}&sz={PAGE_SIZE}"
            )
            page_products = parse_products_html(download_html(page_url))
            if not page_products:
                break
            known = {str(p["id"]) for p in products}
            products.extend(p for p in page_products if str(p["id"]) not in known)
            if len(products) <= start:
                break
            start = len(products)
        if len(products) < expected:
            raise RuntimeError(f"頁面顯示 {expected} 項，但只解析到 {len(products)} 項")
    return products


def download_html(url):
    """下載 HTML，同時限制總時間與大小，避免排程被慢速回應卡住。"""
    started = time.monotonic()
    chunks = []
    size = 0
    with requests.get(url, headers=HEADERS, timeout=(10, 10), stream=True) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"商品頁狀態碼 {resp.status_code}")
        encoding = resp.encoding or "utf-8"
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise RuntimeError("商品頁超過 5 MB，已中止下載")
            if time.monotonic() - started > DOWNLOAD_DEADLINE:
                raise RuntimeError(f"商品頁下載超過 {DOWNLOAD_DEADLINE} 秒")
    return b"".join(chunks).decode(encoding, errors="replace")


def fetch_all_products_with_retry():
    """重試包裝：逾時/連不到時自動重試，每次間隔指數退避。全失敗才丟出例外。"""
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fetch_all_products()
        except Exception as e:
            last_err = e
            if attempt < RETRY_ATTEMPTS:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))  # 2, 4, 8…秒
                print(f"抓取失敗（第 {attempt}/{RETRY_ATTEMPTS} 次）：{e}；{delay:.0f} 秒後重試…")
                time.sleep(delay)
            else:
                print(f"抓取失敗（第 {attempt}/{RETRY_ATTEMPTS} 次，已用盡重試）：{e}")
    raise last_err


def parse_products_html(page_html):
    """從 Salesforce Commerce Cloud 商品卡片解析 SKU、名稱、價格與庫存。"""
    products = []
    for match in PRODUCT_BLOCK_RE.finditer(page_html):
        attrs, body = match.group("attrs"), match.group("body")
        pid_match = re.search(r'data-pid="([^"]+)"', attrs)
        metadata_match = re.search(r"data-metadata='([^']+)'", attrs)
        link_match = re.search(
            r'<a[^>]+class="card-link[^"]*"[^>]+href="([^"]+)"[^>]*>'
            r'.*?<span[^>]+itemprop="name"[^>]*>(.*?)</span>',
            body,
            re.DOTALL,
        )
        if not pid_match or not metadata_match or not link_match:
            continue
        try:
            metadata = json.loads(html.unescape(metadata_match.group(1)))
        except json.JSONDecodeError:
            continue

        title = re.sub(r'<[^>]+>', '', html.unescape(link_match.group(2)))
        title = re.sub(r'\s+', ' ', title).strip()
        status_match = re.search(r'<div class="status">(.*?)</div>', body, re.DOTALL)
        status = ""
        if status_match:
            status = re.sub(r'<[^>]+>', '', html.unescape(status_match.group(1))).strip()

        products.append({
            "id": pid_match.group(1),
            "title": title or metadata.get("name_local") or metadata.get("name"),
            "url": urljoin(SHOP_BASE, html.unescape(link_match.group(1))),
            "price": metadata.get("price"),
            "in_stock": not any(word in status for word in ("暫時缺貨", "缺貨", "Out of Stock")),
        })
    return products


# ============ 解析單筆商品 ============
def _first(prod, fields):
    for f in fields:
        v = prod.get(f)
        if v not in (None, "", []):
            return v
    return None


def detect_in_stock(prod):
    """綜合判斷是否有貨。判不出來時預設 True（寧可不漏，也別誤報缺貨）。"""
    # 明確的布林旗標
    if isinstance(prod.get("available"), bool):
        return prod["available"]
    if isinstance(prod.get("in_stock"), bool):
        return prod["in_stock"]
    if isinstance(prod.get("sold_out"), bool):
        return not prod["sold_out"]
    if isinstance(prod.get("out_of_stock"), bool):
        return not prod["out_of_stock"]
    # 數量型欄位
    for f in ("stock", "quantity", "available_quantity", "inventory", "inventory_quantity"):
        v = prod.get(f)
        if isinstance(v, (int, float)):
            return v > 0
    # 變體（規格）型：任一規格有貨就算有貨
    for f in ("variants", "variations"):
        variants = prod.get(f)
        if isinstance(variants, list) and variants:
            return any(detect_in_stock(v) for v in variants)
    return True  # 未知 → 當作有貨


def _to_number(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def parse_product(prod):
    """轉成統一格式：{key, title, url, price, in_stock}。失敗回 None。"""
    if not isinstance(prod, dict):
        return None
    title = _first(prod, TITLE_FIELDS)
    if not title:
        return None
    title = str(title).strip()

    pid = _first(prod, ID_FIELDS)
    key = str(pid) if pid is not None else title  # ID 優先，沒有才用標題當鍵

    url_path = _first(prod, URL_FIELDS)
    if not url_path and pid is not None:
        url_path = f"/products/{pid}"
    if url_path and not str(url_path).startswith("http"):
        url = f"{SHOP_BASE}{url_path}"
    else:
        url = url_path or SHOP_BASE

    return {
        "key": key,
        "title": title,
        "url": url,
        "price": _to_number(_first(prod, PRICE_FIELDS)),
        "in_stock": detect_in_stock(prod),
    }


# ============ 狀態存取 ============
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def split_meta(state):
    """把監看器自身狀態（META_KEY）從商品狀態裡分出來，回傳 (products, meta)。"""
    products = dict(state)
    meta = products.pop(META_KEY, None)
    if not isinstance(meta, dict):
        meta = {}
    return products, meta


def save_state_with_meta(products, meta):
    """存檔時把商品狀態與 meta 合在一起寫回。"""
    out = dict(products)
    out[META_KEY] = meta
    save_state(out)


def load_watchlist():
    """讀關注清單關鍵字。支援純陣列或 {"keywords":[...]}；讀不到就回空清單。"""
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
    """標題（不分大小寫）命中任一關鍵字就回傳該關鍵字，否則回 None。"""
    t = (title or "").lower()
    for kw in keywords:
        if kw.lower() in t:
            return kw
    return None


def append_history(current, ts):
    """把這次每個商品的快照 append 到 history.jsonl，並保留最近一天。"""
    lines = []
    for p in current.values():
        lines.append(json.dumps({
            "ts": ts,
            "id": p["key"],
            "title": p["title"],
            "price": p.get("price"),
            "in_stock": p.get("in_stock", True),
        }, ensure_ascii=False))
    if lines:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    prune_history(ts)


def _parse_history_ts(value):
    """解析 history.jsonl 裡的 ISO 時間；讀不到就回 None，讓清理流程略過該列。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def prune_history(now_ts):
    """只保留 HISTORY_RETENTION_HOURS 以內的 history.jsonl 紀錄。"""
    if HISTORY_RETENTION_HOURS <= 0 or not os.path.exists(HISTORY_FILE):
        return

    now_dt = _parse_history_ts(now_ts) or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    cutoff = now_dt - timedelta(hours=HISTORY_RETENTION_HOURS)

    tmp_file = f"{HISTORY_FILE}.tmp"
    kept = 0
    removed = 0

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as src, \
                open(tmp_file, "w", encoding="utf-8") as dst:
            for line in src:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    removed += 1
                    continue

                row_ts = _parse_history_ts(row.get("ts"))
                if row_ts is None:
                    removed += 1
                    continue
                if row_ts.tzinfo is None:
                    row_ts = row_ts.replace(tzinfo=timezone.utc)

                if row_ts >= cutoff:
                    dst.write(json.dumps(row, ensure_ascii=False) + "\n")
                    kept += 1
                else:
                    removed += 1
        os.replace(tmp_file, HISTORY_FILE)
        if removed:
            print(f"已清理 history：保留 {kept} 筆，移除 {removed} 筆超過 {HISTORY_RETENTION_HOURS} 小時的紀錄。")
    except OSError as e:
        print(f"清理 history 失敗：{e}")
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except OSError:
            pass


def write_feed(current, new_keys=(), restock_keys=()):
    """產出給 iOS app 讀的清單。new/restock 為這次新增或補貨的商品。"""
    new_keys, restock_keys = set(new_keys), set(restock_keys)
    products = []
    for key, p in current.items():
        if key in new_keys:
            status = "new"
        elif key in restock_keys:
            status = "restock"
        else:
            status = "normal"
        products.append({
            "id": key,
            "title": p["title"],
            "url": p["url"],
            "price": p.get("price"),
            "in_stock": p.get("in_stock", True),
            "status": status,
            "first_seen": p.get("first_seen"),
        })
    # 新的、補貨的排前面，其餘照首次出現時間新到舊
    products.sort(key=lambda x: (
        {"new": 0, "restock": 1, "normal": 2}[x["status"]],
        x["first_seen"] or "",
    ))
    feed = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Toys\"R\"Us Taiwan",
        "count": len(products),
        "products": products,
    }
    with open(FEED_FILE, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)


# ============ ntfy 推播 ============
def ntfy_publish(title, message, tags=None, priority=3, click=None):
    """用 JSON 方式發布，所有中文都放 body，避開 header 編碼問題。"""
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,        # 1=min ... 5=urgent
        "tags": tags or [],
    }
    if click:
        payload["click"] = click
    try:
        resp = requests.post(
            NTFY_SERVER,
            data=json.dumps(payload).encode("utf-8"),
            timeout=10,
        )
        if resp.status_code >= 300:
            print(f"ntfy 回應異常：{resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"發送 ntfy 通知失敗：{e}")


# ============ 主流程 ============
def main():
    print("開始檢查 Beyblade 上架 / 補貨…")

    # 先讀狀態，把連續失敗計數拿出來（抓取成敗都要用到）
    state, meta = split_meta(load_state())
    fail_count = int(meta.get("consecutive_failures", 0) or 0)

    try:
        raw = fetch_all_products_with_retry()
    except Exception as e:
        # 重試都失敗才走到這裡：累計連續失敗次數，存回狀態檔
        fail_count += 1
        meta["consecutive_failures"] = fail_count
        meta["last_failure_at"] = datetime.now(timezone.utc).isoformat()
        meta["last_error"] = str(e)
        save_state_with_meta(state, meta)
        print(f"抓取失敗：{e}（連續第 {fail_count} 次）")

        # 只有連續失敗達門檻才發警告；之後每再累積一輪門檻才再提醒一次，避免洗版
        if fail_count >= FAIL_ALERT_THRESHOLD and \
                (fail_count - FAIL_ALERT_THRESHOLD) % FAIL_ALERT_THRESHOLD == 0:
            ntfy_publish(
                "⚠️ 陀螺監看器連續抓取失敗",
                f"已連續 {fail_count} 次無法讀取玩具反斗城商品頁。\n"
                f"最後錯誤：{e}\n可能是端點改版或被擋，請去看一下。",
                tags=["warning"], priority=4,
            )
        else:
            print(f"未達連續失敗門檻（{FAIL_ALERT_THRESHOLD}），暫不發警告。")
        return

    # 抓取成功：連續失敗計數歸零
    if fail_count:
        print(f"抓取成功，連續失敗計數由 {fail_count} 歸零。")
    meta["consecutive_failures"] = 0
    meta.pop("last_error", None)
    meta.pop("last_failure_at", None)

    if DEBUG and raw:
        print("=== 第一筆商品原始 JSON（用來核對欄位名）===")
        print(json.dumps(raw[0], ensure_ascii=False, indent=2))
        print("=== 解析後 ===")
        print(json.dumps(parse_product(raw[0]), ensure_ascii=False, indent=2))

    current = {}
    for prod in raw:
        p = parse_product(prod)
        if p:
            current[p["key"]] = p

    if not current:
        # 連得上但解析不到（欄位可能對不上）：保留舊商品狀態，但仍把失敗計數歸零
        save_state_with_meta(state, meta)
        print("沒解析到任何商品（欄位可能對不上，用 DEBUG=1 檢查）。")
        return

    now = datetime.now(timezone.utc).isoformat()

    # 每次跑完都記一筆歷史快照（含首次執行），之後用來畫價格走勢／搶手度
    append_history(current, now)

    # 第一次執行：建立基準，不發通知，但仍輸出 feed 讓 app 有東西可顯示
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
            # 補貨：之前缺貨，現在有貨
            if old.get("in_stock") is False and p["in_stock"] is True:
                restocks.append(p)
            # 降價：兩邊都有價格，且更低
            if (NOTIFY_PRICE_DROP and old.get("price") and p["price"]
                    and p["price"] < old["price"]):
                price_drops.append((p, old["price"]))

    send_notifications(new_items, restocks, price_drops, load_watchlist())

    # 自動偵測下架：state 裡有、但這次頁面完全沒出現的商品，判定為已下架。
    # （state 已由 split_meta() 去除 META_KEY，所以不會誤判監看器自身狀態）
    delisted = [old for key, old in state.items() if key not in current]
    notify_delisted(delisted)

    # 只保留這次頁面還看得到的商品（first_seen 已在上面從舊狀態帶過來）。
    # 下架的商品會自動從狀態移除，之後若重新上架就能正確觸發「新上架」通知。
    save_state_with_meta(current, meta)

    # 輸出給 app 的清單，標記這次的新上架與補貨
    write_feed(
        current,
        new_keys=[p["key"] for p in new_items],
        restock_keys=[p["key"] for p in restocks],
    )

    print(f"完成：新上架 {len(new_items)}、補貨 {len(restocks)}、"
          f"降價 {len(price_drops)}、下架 {len(delisted)}。")


def send_starred(p, kind, kw):
    """命中關注清單的商品：醒目高優先通知，與一般通知區隔。"""
    price = f"NT${int(p['price'])}" if p["price"] else ""
    stock = "（有貨）" if p["in_stock"] else "（目前缺貨）"
    ntfy_publish(
        f"[玩具反斗城] 🔔 關注{kind}",
        f"{p['title']}\n命中關鍵字「{kw}」\n{price} {stock}\n點我查看",
        tags=["bell", "star"], priority=5, click=p["url"],
    )


def notify_delisted(delisted):
    """已下架商品：低優先（priority 2）逐項通知。獨立於洗版摘要邏輯之外，
    因為下架屬於低優先資訊，不該被大量新上架/補貨壓掉。"""
    for p in delisted:
        title = p.get("title", p.get("key", "（未知商品）"))
        url = p.get("url")
        ntfy_publish(
            "[玩具反斗城] 🔻已下架",
            f"{title}\n已從玩具反斗城 Beyblade 清單消失。",
            tags=["arrow_down"], priority=2, click=url,
        )


def send_notifications(new_items, restocks, price_drops, keywords=()):
    # 先挑出命中關注清單的新上架／補貨：這些一律單獨發醒目高優先通知
    if keywords:
        starred_restocks = [(p, matched_keyword(p["title"], keywords)) for p in restocks]
        starred_news = [(p, matched_keyword(p["title"], keywords)) for p in new_items]
        for p, kw in starred_restocks:
            if kw:
                send_starred(p, "補貨", kw)
        for p, kw in starred_news:
            if kw:
                send_starred(p, "新上架", kw)
        # 命中的已單獨通知，從一般清單移除，避免重複
        restocks = [p for p, kw in starred_restocks if not kw]
        new_items = [p for p, kw in starred_news if not kw]

    total = len(new_items) + len(restocks) + len(price_drops)
    if total == 0:
        return

    # 事件太多 → 發一則摘要，避免手機被洗版
    if total > FLOOD_THRESHOLD:
        lines = []
        if new_items:
            lines.append(f"🆕 新上架 {len(new_items)} 項")
            lines.extend(f"- {p['title']}" for p in new_items[:5])
            if len(new_items) > 5:
                lines.append(f"...還有 {len(new_items) - 5} 項新上架")
        if restocks:
            lines.append(f"🔁 補貨 {len(restocks)} 項")
            lines.extend(f"- {p['title']}" for p in restocks[:5])
            if len(restocks) > 5:
                lines.append(f"...還有 {len(restocks) - 5} 項補貨")
        if price_drops:
            lines.append(f"📉 降價 {len(price_drops)} 項")
        ntfy_publish("[玩具反斗城] 陀螺大量異動",
                     "\n".join(lines) + "\n打開 app 查看細節。",
                     tags=["bell"], priority=4)
        return

    # 補貨最重要（最高優先），逐項推播並附直達連結
    for p in restocks:
        price = f"NT${int(p['price'])}" if p["price"] else ""
        ntfy_publish(f"[玩具反斗城] 🔁 補貨", f"{p['title']}\n{price}\n點我直接前往購買",
                     tags=["rotating_light"], priority=5, click=p["url"])

    for p in new_items:
        price = f"NT${int(p['price'])}" if p["price"] else ""
        stock = "（有貨）" if p["in_stock"] else "（目前缺貨）"
        ntfy_publish(f"[玩具反斗城] 🆕 新上架", f"{p['title']}\n{price} {stock}\n點我查看",
                     tags=["sparkles"], priority=4, click=p["url"])

    for p, old_price in price_drops:
        ntfy_publish(
            "[玩具反斗城] 📉 降價",
            f"{p['title']}\nNT${int(old_price)} → NT${int(p['price'])}\n點我查看",
            tags=["chart_with_downwards_trend"], priority=3, click=p["url"])


if __name__ == "__main__":
    main()
