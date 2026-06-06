#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Beyblade X 上架 / 補貨偵測引擎  (Funbox / Cyberbiz)

功能：
  - 新上架：清單出現沒看過的商品
  - 補貨  ：原本「缺貨」的商品變成「有貨」
  - 降價  ：價格比上次記錄低（可關閉）
推播：ntfy.sh（手機裝 ntfy app 訂閱一個 topic 即可收到，免費、免帳號）

執行：
  NTFY_TOPIC=你的隨機topic python3 beyblade_watcher.py
第一次跑只會建立基準清單、不發通知（避免洗版）。

核對真實 JSON 欄位（強烈建議第一次先做）：
  DEBUG=1 NTFY_TOPIC=test python3 beyblade_watcher.py
  會印出第一筆商品的原始結構，照著調整下面的 FIELD 設定。
"""

import os
import time
import json
from datetime import datetime, timezone

import requests

# ============ 設定區（都可用環境變數覆蓋，密碼絕不要寫死在程式裡）============
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
# ⚠️ 換成你自己的隨機字串（越亂越好，等於密碼，知道的人都能看到你的通知）
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "beyblade-x-k7m2qz")

API_BASE = "https://shop.funbox.com.tw/category_products/takaratomy/beyblade.json"
SHOP_BASE = "https://shop.funbox.com.tw"
STATE_FILE = os.environ.get("STATE_FILE", "tracked_items.json")
FEED_FILE = os.environ.get("FEED_FILE", "feed.json")  # 給 iOS app 讀的清單

PAGE_LIMIT = 50          # 每頁筆數
MAX_PAGES = 10           # 最多翻幾頁（防呆，避免無限迴圈）
NOTIFY_PRICE_DROP = os.environ.get("NOTIFY_PRICE_DROP", "1") == "1"
FLOOD_THRESHOLD = 15     # 單次事件超過這個數，改發一則摘要避免洗版
DEBUG = os.environ.get("DEBUG", "0") == "1"

# 抓取重試：連不到 Funbox（連線逾時等）時，先重試幾次再算失敗，避免偶發網路抖動。
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
    "Accept": "application/json",
}

# 這些是「常見」欄位名，會依序嘗試。用 DEBUG=1 看過真實 JSON 後再微調。
TITLE_FIELDS = ("title", "name")
ID_FIELDS = ("id", "product_id", "sku", "handle")
URL_FIELDS = ("url", "link", "handle")
PRICE_FIELDS = ("price", "sale_price", "price_min", "min_price", "lowest_price")
# 庫存判斷：不同系統用不同欄位，下面 detect_in_stock() 會綜合判斷


# ============ 抓取 ============
def fetch_all_products():
    """分頁抓取，回傳原始 dict 清單。任何硬錯誤丟出例外給 main 處理。"""
    all_raw = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{API_BASE}?limit={PAGE_LIMIT}&page={page}"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"API 狀態碼 {resp.status_code}（page={page}）")

        data = resp.json()
        page_list = _extract_list(data)
        if not page_list:
            break  # 這頁沒東西，結束分頁
        all_raw.extend(page_list)
        if len(page_list) < PAGE_LIMIT:
            break  # 最後一頁
    return all_raw


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


def _extract_list(data):
    """從各種可能的 JSON 外層結構取出商品陣列。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("products", "items", "data", "results"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        if "title" in data:  # 整包就是單一商品（防呆）
            return [data]
    return []


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
        "source": "Funbox",
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
                f"已連續 {fail_count} 次無法讀取 Funbox API。\n"
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

    send_notifications(new_items, restocks, price_drops)

    # 不在這次清單裡的舊商品保留原狀態（之後再出現可正確判斷補貨）
    merged = dict(state)
    merged.update(current)
    save_state_with_meta(merged, meta)

    # 輸出給 app 的清單，標記這次的新上架與補貨
    write_feed(
        current,
        new_keys=[p["key"] for p in new_items],
        restock_keys=[p["key"] for p in restocks],
    )

    total = len(new_items) + len(restocks) + len(price_drops)
    print(f"完成：新上架 {len(new_items)}、補貨 {len(restocks)}、降價 {len(price_drops)}。")


def send_notifications(new_items, restocks, price_drops):
    total = len(new_items) + len(restocks) + len(price_drops)
    if total == 0:
        return

    # 事件太多 → 發一則摘要，避免手機被洗版
    if total > FLOOD_THRESHOLD:
        lines = []
        if new_items:
            lines.append(f"🆕 新上架 {len(new_items)} 項")
        if restocks:
            lines.append(f"🔁 補貨 {len(restocks)} 項")
        if price_drops:
            lines.append(f"📉 降價 {len(price_drops)} 項")
        ntfy_publish("Funbox 陀螺大量異動",
                     "\n".join(lines) + "\n打開 app 查看細節。",
                     tags=["bell"], priority=4)
        return

    # 補貨最重要（最高優先），逐項推播並附直達連結
    for p in restocks:
        price = f"NT${int(p['price'])}" if p["price"] else ""
        ntfy_publish(f"🔁 補貨！{p['title']}", f"{price}\n點我直接前往購買",
                     tags=["rotating_light"], priority=5, click=p["url"])

    for p in new_items:
        price = f"NT${int(p['price'])}" if p["price"] else ""
        stock = "（有貨）" if p["in_stock"] else "（目前缺貨）"
        ntfy_publish(f"🆕 新上架：{p['title']}", f"{price} {stock}\n點我查看",
                     tags=["sparkles"], priority=4, click=p["url"])

    for p, old_price in price_drops:
        ntfy_publish(
            f"📉 降價：{p['title']}",
            f"NT${int(old_price)} → NT${int(p['price'])}\n點我查看",
            tags=["chart_with_downwards_trend"], priority=3, click=p["url"])


if __name__ == "__main__":
    main()
