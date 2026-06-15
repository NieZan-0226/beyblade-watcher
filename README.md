# beyblade-watcher

Beyblade X 上架 / 補貨偵測器 — 監看 [Funbox](https://shop.funbox.com.tw) 的戰鬥陀螺商品，
有**新上架、補貨、降價**時透過 [ntfy](https://ntfy.sh) 推播到手機，並產出一個網頁看板。

## 🔗 線上看板

**https://beybladewatcher-tw.netlify.app**

看板資料來自 `feed.json`，由 GitHub Actions 每分鐘自動更新後推回 repo，
Netlify 偵測到更新即自動重新部署。

## 運作方式

- `beyblade_watcher.py` — 抓取 Funbox API，比對上次的狀態（存在 `tracked_items.json`），
  偵測新上架 / 補貨 / 降價並推播；同時輸出 `feed.json` 給看板顯示。
  - 連不到 Funbox 時會自動重試（指數退避），連續失敗達門檻才發警告，避免偶發逾時洗版。
- `.github/workflows/watch.yml` — 排程每 15 分鐘執行一次，並把更新後的狀態檔 commit 回 repo。
- `index.html` + `icon-180.png` + `feed.json` — 純靜態看板，由 Netlify 發佈（設定見 `netlify.toml`）。

## 安裝

先把專案 clone 下來：

```bash
git clone https://github.com/NieZan-0226/beyblade-watcher.git
cd beyblade-watcher
```

安裝 Python 依賴：

```bash
python3 -m pip install -r requirements.txt
```

先手動跑一次確認可以正常抓資料與推播：

```bash
NTFY_TOPIC=beyblade-x-k7m2qz python3 beyblade_watcher.py
```

`NTFY_TOPIC` 可換成自己的 ntfy topic。若要看 API 原始欄位，可加 `DEBUG=1`：

```bash
DEBUG=1 NTFY_TOPIC=beyblade-x-k7m2qz python3 beyblade_watcher.py
```

## 排程執行

`history.jsonl` 會由程式自動清理，只保留最近 24 小時的資料。若要改保留時間，可設定：

```bash
HISTORY_RETENTION_HOURS=24
```

### Ubuntu / Debian

安裝必要套件：

```bash
sudo apt update
sudo apt install -y git python3 python3-pip
```

編輯 crontab：

```bash
crontab -e
```

每分鐘執行一次，log 依日期分檔，並刪除超過 24 小時的 log。注意 crontab 裡的 `%` 要寫成 `\%`：

```cron
*/1 * * * * cd /home/nspectrum/beyblade-watcher && NTFY_TOPIC=beyblade-x-k7m2qz /usr/bin/python3 beyblade_watcher.py >> watcher-$(date +\%F).log 2>&1
17 * * * * find /home/nspectrum/beyblade-watcher -name 'watcher-*.log' -mmin +1440 -delete
```

請把 `/home/nspectrum/beyblade-watcher` 換成實際 clone 的路徑。

### Red Hat / Rocky / AlmaLinux / CentOS

安裝必要套件：

```bash
sudo dnf install -y git python3 python3-pip
```

若系統較舊、沒有 `dnf`，改用：

```bash
sudo yum install -y git python3 python3-pip
```

編輯 crontab：

```bash
crontab -e
```

加入：

```cron
*/1 * * * * cd /home/nspectrum/beyblade-watcher && NTFY_TOPIC=beyblade-x-k7m2qz /usr/bin/python3 beyblade_watcher.py >> watcher-$(date +\%F).log 2>&1
17 * * * * find /home/nspectrum/beyblade-watcher -name 'watcher-*.log' -mmin +1440 -delete
```

請把 `/home/nspectrum/beyblade-watcher` 換成實際 clone 的路徑。

### Windows

先安裝 Git 與 Python，然後用 PowerShell clone：

```powershell
git clone https://github.com/NieZan-0226/beyblade-watcher.git
cd beyblade-watcher
py -m pip install -r requirements.txt
```

建立每日 log 目錄：

```powershell
New-Item -ItemType Directory -Force -Path "$PWD\logs"
```

用「工作排程器」建立每分鐘執行一次：

1. 開啟「工作排程器」。
2. 選「建立工作」。
3. 在「觸發程序」新增「每天」，進階設定勾選「重複工作間隔：1 分鐘」。
4. 在「動作」新增「啟動程式」。
5. 程式填 `powershell.exe`。
6. 引數填入下方內容，並把路徑換成你的實際路徑：

```powershell
-NoProfile -ExecutionPolicy Bypass -Command "cd 'C:\beyblade-watcher'; $env:NTFY_TOPIC='beyblade-x-k7m2qz'; py .\beyblade_watcher.py >> .\logs\watcher-$(Get-Date -Format yyyy-MM-dd).log 2>&1"
```

再建立一個每天清理舊 log 的工作，動作一樣使用 `powershell.exe`，引數填：

```powershell
-NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem 'C:\beyblade-watcher\logs' -Filter 'watcher-*.log' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } | Remove-Item"
```

請把 `C:\beyblade-watcher` 換成實際 clone 的路徑。

如果想沿用單一 `watcher.log`，也可以每天凌晨清空一次：

```cron
0 0 * * * : > /home/nspectrum/beyblade-watcher/watcher.log
```

## 部署

看板為純靜態網站，Netlify 直接發佈 repo 根目錄、無需 build（見 `netlify.toml`）。
已連結 GitHub 自動部署：推送到 `main`（含監看器更新 `feed.json`）即自動重新上線。
