# beyblade-watcher

Beyblade X 上架 / 補貨偵測器 — 監看 [Funbox](https://shop.funbox.com.tw) 的戰鬥陀螺商品，
有**新上架、補貨、降價**時透過 [ntfy](https://ntfy.sh) 推播到手機，並產出一個網頁看板。

## 🔗 線上看板

**https://beybladewatcher-tw.netlify.app**

看板資料來自 `feed.json`，由 GitHub Actions 每 15 分鐘自動更新後推回 repo，
Netlify 偵測到更新即自動重新部署。

## 運作方式

- `beyblade_watcher.py` — 抓取 Funbox API，比對上次的狀態（存在 `tracked_items.json`），
  偵測新上架 / 補貨 / 降價並推播；同時輸出 `feed.json` 給看板顯示。
  - 連不到 Funbox 時會自動重試（指數退避），連續失敗達門檻才發警告，避免偶發逾時洗版。
- `.github/workflows/watch.yml` — 排程每 15 分鐘執行一次，並把更新後的狀態檔 commit 回 repo。
- `index.html` + `icon-180.png` + `feed.json` — 純靜態看板，由 Netlify 發佈（設定見 `netlify.toml`）。

## 部署

看板為純靜態網站，Netlify 直接發佈 repo 根目錄、無需 build（見 `netlify.toml`）。
已連結 GitHub 自動部署：推送到 `main`（含監看器更新 `feed.json`）即自動重新上線。

## VM / crontab

`history.jsonl` 會由程式自動清理，只保留最近 24 小時的資料。若要改保留時間，可設定：

```bash
HISTORY_RETENTION_HOURS=24
```

VM 上建議把 log 依日期分檔，再用 `find` 刪掉超過一天的檔案。注意 crontab 裡的 `%` 要寫成 `\%`：

```cron
*/1 * * * * cd /home/nspectrum/beyblade-watcher && NTFY_TOPIC=beyblade-x-k7m2qz /usr/bin/python3 beyblade_watcher.py >> watcher-$(date +\%F).log 2>&1
17 * * * * find /home/nspectrum/beyblade-watcher -name 'watcher-*.log' -mmin +1440 -delete
```

如果想沿用單一 `watcher.log`，也可以每天凌晨清空一次：

```cron
0 0 * * * : > /home/nspectrum/beyblade-watcher/watcher.log
```
