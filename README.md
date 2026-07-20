# beyblade-watcher / TheWatcher

Beyblade 商品上架、補貨、降價與下架偵測器，一次監控四個來源：

- [Funbox 戰鬥陀螺](https://shop.funbox.com.tw/categories/takaratomy/beyblade)
- [玩具反斗城 Beyblade](https://www.toysrus.com.tw/zh-tw/beyblade/)
- [誠品線上 BEYBLADE X 策展頁](https://www.eslite.com/exhibitions/CU202503-00091)
- [森森文具玩具 戰鬥陀螺X](https://www.sen-sen.com.tw/collections/%E6%88%B0%E9%AC%A5%E9%99%80%E8%9E%BAx)

異動時會透過 [ntfy](https://ntfy.sh) 推播到手機，並產生一個合併的靜態網頁看板。四個來源各自保留狀態與歷史，其中一站暫時故障時，不會阻斷其他來源的檢查。

## 線上看板

<https://beybladewatcher-tw.netlify.app>

看板讀取 `feed.json` 與 `history.jsonl`。GitHub Actions 每 15 分鐘執行監控器並將新狀態 commit 回 repository，Netlify 偵測到更新後會自動重新部署。

## 功能

- 新上架：清單出現以前沒看過的商品
- 補貨：原本缺貨的商品變為有貨
- 降價：價格低於上次記錄
- 下架：商品從來源清單消失
- 關鍵字關注：命中 `watchlist.json` 的商品會發送高優先通知
- 失敗重試：偶發連線失敗會指數退避重試，連續失敗達門檻才警告
- 合併看板：同時顯示 Funbox、玩具反斗城、誠品線上與森森文具玩具商品
- 手機通知：標題保持短，完整商品名放在訊息第一行，避免 iOS 橫幅截斷後看不到補貨商品

## 檔案說明

- `the_watcher.py`：整合入口，一次執行四個監控器並合併看板資料
- `funbox_watcher.py`：Funbox 監控器
- `toysrus_watcher.py`：玩具反斗城監控器
- `eslite_watcher.py`：誠品線上策展頁監控器
- `sensen_watcher.py`：森森文具玩具監控器
- `funbox_tracked_items.json`：Funbox 上次商品快照
- `toysrus_tracked_items.json`：玩具反斗城上次商品快照
- `eslite_tracked_items.json`：誠品線上上次商品快照
- `sensen_tracked_items.json`：森森文具玩具上次商品快照
- `funbox_feed.json` / `toysrus_feed.json` / `eslite_feed.json` / `sensen_feed.json`：各來源的看板資料
- `feed.json`：四個來源合併後的看板資料
- `funbox_history.jsonl` / `toysrus_history.jsonl` / `eslite_history.jsonl` / `sensen_history.jsonl`：各來源最近 24 小時歷史
- `history.jsonl`：合併後的價格與庫存歷史
- `watchlist.json`：四站共用的關注關鍵字
- `index.html`：靜態網頁看板

## 安裝

先安裝 Git 與 Python 3.10 以上版本，再 clone repository：

```bash
git clone https://github.com/NieZan-0226/beyblade-watcher.git
cd beyblade-watcher
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

Windows 可使用：

```powershell
git clone https://github.com/NieZan-0226/beyblade-watcher.git
cd beyblade-watcher
py -m pip install -r requirements.txt
py -m playwright install chromium
```

## ntfy 推播設定

1. 在手機安裝 ntfy App。
2. 自行想一個隨機且難猜的 topic，例如 `beyblade-x-a8f3k9m2`。
3. 在 ntfy App 訂閱該 topic。
4. 執行監控器時將 topic 放入 `NTFY_TOPIC`。

topic 相當於通知頻道密碼，請勿使用過於簡單或公開的字串。

### 四站共用同一個 topic

Linux / macOS：

```bash
NTFY_TOPIC="你的隨機-topic" python3 the_watcher.py
```

Windows PowerShell：

```powershell
$env:NTFY_TOPIC = "你的隨機-topic"
py .\the_watcher.py
```

### 四站分開 topic

Linux / macOS：

```bash
FUNBOX_NTFY_TOPIC="funbox-topic" TOYSRUS_NTFY_TOPIC="toysrus-topic" ESLITE_NTFY_TOPIC="eslite-topic" SENSEN_NTFY_TOPIC="sensen-topic" python3 the_watcher.py
```

Windows PowerShell：

```powershell
$env:FUNBOX_NTFY_TOPIC = "funbox-topic"
$env:TOYSRUS_NTFY_TOPIC = "toysrus-topic"
$env:ESLITE_NTFY_TOPIC = "eslite-topic"
$env:SENSEN_NTFY_TOPIC = "sensen-topic"
py .\the_watcher.py
```

也可以讓同一個來源同時發送到多個 topic，使用逗號分隔即可。例如 Funbox 同時發到共用頻道與 Funbox 專用頻道：

```bash
NTFY_TOPIC="beyblade-x-k7m2qz" FUNBOX_NTFY_TOPIC="funbox-beyblade-x-k9m4p7q2,beyblade-x-k7m2qz" python3 the_watcher.py
```

### Debug 模式

如果網站改版導致解析異常，可開啟 Debug：

```bash
DEBUG=1 NTFY_TOPIC="你的-topic" python3 the_watcher.py
```

## Discord Webhook 通知

如果你想讓電腦 Discord 也跳通知，可以在 Discord 頻道建立 Webhook，然後執行時設定 `DISCORD_WEBHOOK_URL`。未設定時會照舊只發 ntfy。

如果要同時發到多個 Discord 伺服器 / 頻道，把多個 Webhook URL 用逗號分隔即可。Discord 標所有人的語法是 `@everyone`，不是 `@ALL`；要真的 ping 所有人，請加上 `DISCORD_MENTION="@everyone"`。

Linux / macOS：

```bash
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/xxx/yyy" NTFY_TOPIC="你的-topic" python3 the_watcher.py
```

同時發到兩個 Discord webhook，並標所有人：

```bash
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/xxx/yyy,https://discord.com/api/webhooks/aaa/bbb" DISCORD_MENTION="@everyone" NTFY_TOPIC="你的-topic" python3 the_watcher.py
```

搭配 Funbox 同時傳兩個 ntfy topic：

```bash
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/xxx/yyy,https://discord.com/api/webhooks/aaa/bbb" DISCORD_MENTION="@everyone" NTFY_TOPIC="beyblade-x-k7m2qz" FUNBOX_NTFY_TOPIC="funbox-beyblade-x-k9m4p7q2,beyblade-x-k7m2qz" python3 the_watcher.py
```

cron 範例：

```cron
*/1 * * * * cd /home/clmadmin/beyblade-watcher && DISCORD_WEBHOOK_URL="你的-discord-webhook-url-1,你的-discord-webhook-url-2" DISCORD_MENTION="@everyone" NTFY_TOPIC=beyblade-x-k7m2qz FUNBOX_NTFY_TOPIC="funbox-beyblade-x-k9m4p7q2,beyblade-x-k7m2qz" /usr/bin/python3 the_watcher.py >> watcher-$(date +\%F).log 2>&1
```

Webhook URL 等同於發送權限，請不要公開貼到 GitHub 或聊天室。

## 關注清單

編輯 `watchlist.json`，放入想特別關注的商品關鍵字：

```json
[
  "CX-",
  "鳳凰",
  "隨機強化組"
]
```

標題命中關鍵字的新上架或補貨商品，會使用高優先級通知。

## GitHub Actions 排程

repository 已包含 `.github/workflows/watch.yml`，預設每 15 分鐘執行一次：

```yaml
schedule:
  - cron: '5,20,35,50 * * * *'
```

### 設定 GitHub Secret

1. 進入 GitHub repository。
2. 開啟 `Settings` → `Secrets and variables` → `Actions`。
3. 點選 `New repository secret`。
4. Name 填入 `NTFY_TOPIC`。
5. Secret 填入手機 ntfy App 訂閱的 topic。

請務必設定 `NTFY_TOPIC`，不要把私人 topic 直接寫進程式或 workflow。

如果要 GitHub Actions 也發 Discord，另外新增 Secret：

- Name：`DISCORD_WEBHOOK_URL`
- Secret：你的 Discord Webhook URL；多個 URL 可用逗號分隔

如果 GitHub Actions 的 Discord 通知也要標所有人，另外新增 Secret：

- Name：`DISCORD_MENTION`
- Secret：`@everyone`

### 手動測試 Actions

1. 進入 repository 的 `Actions`。
2. 選擇 `TheWatcher`。
3. 點選 `Run workflow`。
4. 如果要測試 ntfy，勾選「發送測試通知到手機」。

Actions 執行後會將四站的狀態檔、歷史與合併 feed commit 回 `main`。

## Linux cron 排程

GitHub Actions 與本機 cron 擇一使用即可，不建議同時啟用，否則可能重複通知。

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y git python3 python3-pip
git clone https://github.com/NieZan-0226/beyblade-watcher.git
cd beyblade-watcher
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

編輯 crontab：

```bash
crontab -e
```

每分鐘執行一次，並將 log 依日期分檔：

```cron
* * * * * cd /home/USER/beyblade-watcher && NTFY_TOPIC="你的-topic" /usr/bin/python3 the_watcher.py >> watcher-$(date +\%F).log 2>&1
17 * * * * find /home/USER/beyblade-watcher -name 'watcher-*.log' -mmin +1440 -delete
```

crontab 內的 `%` 必須寫成 `\%`，並將 `/home/USER/beyblade-watcher` 改為實際路徑。

### Red Hat / Rocky / AlmaLinux / CentOS

```bash
sudo dnf install -y git python3 python3-pip
git clone https://github.com/NieZan-0226/beyblade-watcher.git
cd beyblade-watcher
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

若系統沒有 `dnf`，可改用：

```bash
sudo yum install -y git python3 python3-pip
```

排程設定與 Ubuntu 相同：

```cron
* * * * * cd /home/USER/beyblade-watcher && NTFY_TOPIC="你的-topic" /usr/bin/python3 the_watcher.py >> watcher-$(date +\%F).log 2>&1
17 * * * * find /home/USER/beyblade-watcher -name 'watcher-*.log' -mmin +1440 -delete
```

## Windows 工作排程器

先建立 log 目錄：

```powershell
New-Item -ItemType Directory -Force -Path "$PWD\logs"
```

1. 開啟 Windows「工作排程器」。
2. 選擇「建立工作」。
3. 在「觸發程序」新增「每日」，並設定每 1 分鐘重複。
4. 在「動作」新增「啟動程式」。
5. 程式填入 `powershell.exe`。
6. 引數填入下方內容，並將路徑與 topic 改為自己的設定：

```powershell
-NoProfile -ExecutionPolicy Bypass -Command "cd 'C:\beyblade-watcher'; $env:NTFY_TOPIC='你的-topic'; py .\the_watcher.py >> .\logs\watcher-$(Get-Date -Format yyyy-MM-dd).log 2>&1"
```

可再建立一個每日執行的清理工作：

```powershell
-NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem 'C:\beyblade-watcher\logs' -Filter 'watcher-*.log' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } | Remove-Item"
```

## 環境變數

- `NTFY_TOPIC`：四站共用的 ntfy topic
- `FUNBOX_NTFY_TOPIC`：僅 Funbox 使用的 topic，會覆蓋 `NTFY_TOPIC`
- `TOYSRUS_NTFY_TOPIC`：僅玩具反斗城使用的 topic，會覆蓋 `NTFY_TOPIC`
- `ESLITE_NTFY_TOPIC`：僅誠品線上使用的 topic，會覆蓋 `NTFY_TOPIC`
- `SENSEN_NTFY_TOPIC`：僅森森文具玩具使用的 topic，會覆蓋 `NTFY_TOPIC`
- `DISCORD_WEBHOOK_URL`：Discord Webhook URL；未設定時不發 Discord，多個 URL 可用逗號分隔
- `DISCORD_USERNAME`：Discord Webhook 顯示名稱，預設 `TheWatcher`
- `DISCORD_MENTION`：Discord 訊息最上方要帶的 mention，例如 `@everyone`；未設定時不標人
- `NTFY_SERVER`：ntfy 伺服器，預設為 `https://ntfy.sh`
- `ESLITE_EXHIBITION_URL`：誠品線上策展頁網址，預設為 BEYBLADE X 策展頁
- `ESLITE_NTFY_CLICK_URL`：誠品線上 ntfy 通知點擊後開啟的網址，預設 `https://reurl.cc/xWdQOe`
- `SENSEN_COLLECTION_API`：森森文具玩具分類 JSON API，預設為戰鬥陀螺X 分類
- `NOTIFY_PRICE_DROP`：是否發送降價通知，`1` 為開啟，`0` 為關閉
- `HISTORY_RETENTION_HOURS`：歷史保留小時數，預設 `24`
- `RETRY_ATTEMPTS`：抓取失敗時的總嘗試次數，預設 `3`
- `FAIL_ALERT_THRESHOLD`：連續失敗幾次後才發送警告，預設 `4`
- `DEBUG`：設為 `1` 時輸出原始商品資料協助除錯

## Netlify 部署

看板為純靜態網站，`netlify.toml` 已設定直接發佈 repository 根目錄，不需要 build command。

1. 在 Netlify 新增專案並連結這個 GitHub repository。
2. Publish directory 使用 `.`。
3. Build command 留空。
4. GitHub Actions 更新 `feed.json` 後，Netlify 會自動重新部署。

`feed.json` 已設定為不長期快取，看板也會在請求時加入 timestamp，確保顯示最新商品狀態。
