# polymarket-bot-by_openclaw

Polymarket 5-minute BTC market trading bot.

這個 repo 目前以「dry-run 優化」為主，不是包裝過的量產版產品。現在的重點是把進場、風控、分批出場、報表和 journaling 這條鏈路打磨穩定，再決定哪些參數要收斂回來。

## 現在這版在做什麼

- 自動切到最新的 Polymarket `btc-updown-5m-*` 市場
- 用 Binance WebSocket 資料輔助做方向判斷
- 只允許同一時間持有一個開倉部位
- 進場前會檢查價格區間、進場時窗、模型 edge、流動性、網路狀態
- 出場支援分批停損、抽本金、deadline flat/loss close、stop-loss
- 結束執行時會自動生成 run report

## 目前預設

目前 repo 內建的預設值是偏「優化模式」：

- `DRY_RUN=true`
- `MAX_ORDER_USD=1.0`
- `AUTO_MARKET_SELECTION=true`
- `MAX_EXPOSURE_USD` / `MAX_CONSEC_LOSS` / `DAILY_MAX_LOSS` 已刻意設很高，避免優化階段太早被風控打斷

也就是說，這份設定比較適合觀察策略行為，不是保守實盤設定。

## 安裝

```bash
git clone https://github.com/Chihen-Tai/polymarket_bot_for_5_min_btc.git
cd polymarket_bot_for_5_min_btc
conda env create -f environment.yml
conda activate polymarket-bot
```

如果你本來就在舊 repo 路徑上工作，也可以直接沿用現有 clone。

## 設定 `.env`

這個 repo 目前保留一份追蹤中的 `.env` 當共用基礎設定，但 GitHub 使用者不要直接把它當成 live secrets 檔。

建議做法：

- dry-run：參考 [.env.example](/Applications/codes/polymarket-bot-by_openclaw/.env.example)
- live：參考 [.env.live.example](/Applications/codes/polymarket-bot-by_openclaw/.env.live.example)
- 敏感值：從 [.env.secrets.example](/Applications/codes/polymarket-bot-by_openclaw/.env.secrets.example) 複製到你自己的 `.env.local` 或 `.env.secrets`
- 程式會依序讀取：追蹤中的 `.env` -> 本機 `.env.local` -> 本機 `.env.secrets`
- 所以你自己的私鑰和 API 憑證只放在本機覆蓋檔，不要 commit

另外，bot 現在會自動把 `dry-run` 和 `live` 分開寫到不同的 runtime state / trade journal / run journal，不再共用同一份本地「記憶」。

目前 bot 真正會讀的 live 相關欄位主要是：

- `DRY_RUN`
- `PRIVATE_KEY`
- `FUNDER_ADDRESS`
- `SIGNATURE_TYPE`
- `CLOB_API_KEY`
- `CLOB_API_SECRET`
- `CLOB_API_PASSPHRASE`
- `AUTO_MARKET_SELECTION`
- `TOKEN_ID_UP`
- `TOKEN_ID_DOWN`

實盤常見的 `invalid signature` 幾乎都來自這三個不匹配：

- `PRIVATE_KEY`
- `FUNDER_ADDRESS`
- `SIGNATURE_TYPE`

`SIGNATURE_TYPE` 的用法：

- `0`：一般 EOA 錢包
- `1`：email / Magic wallet
- `2`：proxy wallet / smart wallet

如果你是 proxy / smart wallet，通常要同時提供：

- 正確的 `SIGNATURE_TYPE=2`
- 真正持有資金的 `FUNDER_ADDRESS`
- 與這組 signer / funder 對應的 `CLOB_API_*`

如果不確定 `CLOB_API_*` 是否過期，可以先清空，讓 bot 重新 derive。

這個 repo 目前不會讀這些欄位，所以就算你填了也不會影響 bot：

- `RELAYER_URL`
- `RELAYER_API_KEY`
- `RELAYER_API_KEY_ADDRESS`
- `PROXY_WALLET`
- `BUILDER_KEY`
- `BUILDER_SECRET`
- `BUILDER_PASSPHRASE`

重要提醒：

- live 前先把 `DRY_RUN=false`
- 先確認 `.env.live.example` 裡的欄位和你的帳戶型態一致
- 如果本機填入真實私鑰或 API 憑證，不要把那些內容再 commit 上去

## 執行

只啟動 bot：

```bash
python main.py
```

只啟動 market data collector：

```bash
bash scripts/start_market_data_collector.sh
```

同時啟動 collector + bot：

```bash
bash scripts/start_bot_with_market_data.sh
```

程式啟動後會：

- 把 console 輸出同步寫到 `data/log-<mode>-<timestamp>.txt`
- 持續輪詢 / 監控最新市場
- 在結束時自動產生報表

`start_bot_with_market_data.sh` 會自動：

- `conda activate polymarket-bot`
- 先啟動 `market_data_collector`
- 再啟動 bot
- bot 結束時一併關掉 collector

常用參數：

```bash
bash scripts/start_market_data_collector.sh --mode dryrun --poll-sec 0.5
bash scripts/start_market_data_collector.sh --background
bash scripts/start_bot_with_market_data.sh --collector-poll-sec 0.5
bash scripts/start_bot_with_market_data.sh --skip-collector
```

## 執行後產物

每次執行後，常用輸出會在 `data/`：

- `log-dryrun-*.txt` 或 `log-live-*.txt`
- `report-dryrun-*.txt` 或 `report-live-*.txt`
- `latest_run_report.txt`

`latest_run_report.txt` 是最快速查看最近一次結果的檔案。

如果有啟動 market data collector，額外輸出會在 `market_data/`：

- `market_data/YYYY-MM-DD/<event-folder>/event.json`
- `market_data/YYYY-MM-DD/<event-folder>/window.jsonl`
- `market_data/logs/collector-*.log`
- `market_data/logs/stack-collector-*.log`

collector 只會讀 journal、抓市場快照並寫入 `market_data/`，不會改 bot 的交易邏輯或下單流程。

## Live Notes

如果你要先用很小部位實戰，建議：

- 先從 [.env.live.example](/Applications/codes/polymarket-bot-by_openclaw/.env.live.example) 開始
- 再把 [.env.secrets.example](/Applications/codes/polymarket-bot-by_openclaw/.env.secrets.example) 複製成 `.env.local` 或 `.env.secrets`
- 保持 `MAX_ORDER_USD=1.0`
- 先跑小量 live 驗證 signer / funder / API creds 都對

如果 log 出現 `invalid signature`，先檢查：

- `SIGNATURE_TYPE` 是否正確
- `FUNDER_ADDRESS` 是否真的是 Polymarket 上持有資金的地址
- `CLOB_API_*` 是否屬於同一組 signer / funder

如果 log 出現 live limit order 送單錯誤，確認你已經拉到包含 `GTC` limit-order 相容修正的版本。

## 常用報表指令

看最近交易配對與 summary：

```bash
python scripts/trade_pair_ledger.py --limit 30 --summary
```

驗證 actual / observed close accounting：

```bash
python scripts/verify_close_accounting.py --limit 30 --summary
```

匯出 CSV / JSON 也可以：

```bash
python scripts/trade_pair_ledger.py --limit 50 --format csv --output data/trade_ledger.csv
python scripts/verify_close_accounting.py --limit 50 --format json --output data/close_accounting.json
```

## 測試

目前 repo 內有幾組核心 smoke tests：

```bash
conda run -n polymarket-bot python -m pytest -q tests/test_trade_manager.py tests/test_exit_fix.py tests/test_market_data_collector.py tests/test_runtime_paths.py
```

## 專案結構

- `main.py`: 啟動入口，負責建立 log tee
- `core/runner.py`: 主迴圈、進出場協調、run report 生成
- `core/decision_engine.py`: 訊號與方向判斷
- `core/trade_manager.py`: 出場規則、分批處理、re-entry gate
- `core/exchange.py`: dry-run / live exchange 介面與部位帳務
- `core/config.py`: 所有 `.env` 設定載入
- `scripts/trade_pair_ledger.py`: 交易配對報表
- `scripts/verify_close_accounting.py`: 平倉對帳檢查
- `scripts/market_data_collector.py`: 收集買賣前後 30 秒市場資料
- `scripts/start_market_data_collector.sh`: 啟動 collector 的 shell wrapper
- `scripts/start_bot_with_market_data.sh`: 一鍵啟動 collector + bot
- `config_presets/dryrun_aggressive.env`: 目前主要的 dry-run preset

## 建議工作流程

1. 先用 `DRY_RUN=true`
2. 每次只改少數幾個參數
3. 跑一輪後先看 `data/latest_run_report.txt`
4. 再回頭看 `trade_pair_ledger` 和 log，確認是「真的改善」還是只是減少交易數
5. 如果要切實盤，先把高到幾乎關閉的風控門檻收回來

## 注意

- 這是高風險交易工具，不是投資建議
- Polymarket、Data API、CLOB、Binance WS 任一端延遲或異常都可能影響結果
- repo 目前仍在快速調整，README 會跟著策略演進更新

---

## English Summary

This repository is an event-driven trading bot for Polymarket 5-minute BTC markets. The current branch is tuned for dry-run optimization, not conservative live deployment.

Quick start:

```bash
git clone https://github.com/Chihen-Tai/polymarket_bot_for_5_min_btc.git
cd polymarket_bot_for_5_min_btc
conda env create -f environment.yml
conda activate polymarket-bot
python main.py
```

Collector only:

```bash
bash scripts/start_market_data_collector.sh
```

Collector + bot:

```bash
bash scripts/start_bot_with_market_data.sh
```

Important notes:

- Keep shared defaults in the tracked `.env`, and put secrets in `.env.local` / `.env.secrets`
- Default settings are currently optimization-oriented
- Logs and reports are written under `data/`
- Market snapshots around fills are written under `market_data/`
- The fastest post-run check is `data/latest_run_report.txt`
