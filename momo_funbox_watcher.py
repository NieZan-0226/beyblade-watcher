#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Beyblade 上架 / 補貨偵測入口（MOMO Funbox TAKARA TOMY / Beyblade 戰鬥陀螺）。"""

import os


os.environ["MOMO_SOURCE_NAME"] = os.environ.get("MOMO_FUNBOX_SOURCE_NAME", "MOMO Funbox")
os.environ["MOMO_SHOP_URL"] = os.environ.get(
    "MOMO_FUNBOX_SHOP_URL",
    "https://www.momoshop.com.tw/categories/2186500000?brandName=TAKARA+TOMY&brandNoList=20190717104950516&brandSeriesStr=Beyblade+%E6%88%B0%E9%AC%A5%E9%99%80%E8%9E%BA%23%2320220916001674&has3P=y",
)
os.environ["MOMO_KEYWORD_RE"] = os.environ.get("MOMO_FUNBOX_KEYWORD_RE", r"BEYBLADE|戰鬥陀螺")
os.environ.setdefault("MOMO_FETCH_MODE", "rendered")
os.environ.setdefault("STATE_FILE", "momo_funbox_tracked_items.json")
os.environ.setdefault("FEED_FILE", "momo_funbox_feed.json")
os.environ.setdefault("HISTORY_FILE", "momo_funbox_history.jsonl")
if os.environ.get("MOMO_FUNBOX_USE_PRODUCT_IDS", "1").strip().lower() not in {"0", "false", "no", "off"}:
    os.environ.setdefault("MOMO_PRODUCT_IDS_FILE", "momo_funbox_product_ids.json")
os.environ.setdefault("NTFY_TOPIC", os.environ.get("MOMO_FUNBOX_NTFY_TOPIC") or "momo-funbox-beyblade-k7m2qz")


from momo_watcher import main  # noqa: E402


if __name__ == "__main__":
    main()
