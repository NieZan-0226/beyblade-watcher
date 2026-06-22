# TheWatcher

一次監控兩個 Beyblade 商品來源：

- Funbox：<https://shop.funbox.com.tw/categories/takaratomy/beyblade>
- 玩具反斗城：<https://www.toysrus.com.tw/zh-tw/beyblade/>

兩個來源各自保留狀態和歷史，其中一站暫時無法連線時，不會影響另一站的檢查。執行後會合併產生 `feed.json` 與 `history.jsonl` 供同一個看板使用。

## 執行

```powershell
cd beyblade-watcher
py -m pip install -r requirements.txt
$env:NTFY_TOPIC = "你的 ntfy topic"
py .\the_watcher.py
```

兩站預設共用 `NTFY_TOPIC`。如果要分開推播，可另設：

```powershell
$env:FUNBOX_NTFY_TOPIC = "funbox-topic"
$env:TOYSRUS_NTFY_TOPIC = "toysrus-topic"
```

## 排程與看板

`.github/workflows/watch.yml` 每 15 分鐘執行一次。放到獨立 GitHub repo 後，請新增 Actions secret `NTFY_TOPIC`。

`index.html` 可直接部署到 Netlify，會讀取合併後的 `feed.json` 和 `history.jsonl`。
