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

這個 repo 現在是直接追蹤 `.env`，不是用 `.env.example` 複製出來。

你要做的是直接修改 repo 根目錄的 `.env`。

最常改的欄位：

- `DRY_RUN`
- `AUTO_MARKET_SELECTION`
- `PRIVATE_KEY`
- `FUNDER_ADDRESS`
- `CLOB_API_KEY`
- `CLOB_API_SECRET`
- `CLOB_API_PASSPHRASE`
- `MAX_ORDER_USD`
- `ENTRY_WINDOW_MIN_SEC`
- `ENTRY_WINDOW_MAX_SEC`
- `MIN_ENTRY_PRICE`
- `MAX_ENTRY_PRICE`
- `STOP_LOSS_*`
- `TAKE_PROFIT_*`

重要提醒：

- repo 內的 `.env` 預設會保留空白敏感欄位
- 如果你本機填入真實私鑰或 API 憑證，不要把那些內容再 commit 上去
- 要切實盤時，把 `DRY_RUN=false`，並先再次確認金鑰和地址

## 執行

```bash
python main.py
```

程式啟動後會：

- 把 console 輸出同步寫到 `data/log-<mode>-<timestamp>.txt`
- 持續輪詢 / 監控最新市場
- 在結束時自動產生報表

## 執行後產物

每次執行後，常用輸出會在 `data/`：

- `log-dryrun-*.txt` 或 `log-live-*.txt`
- `report-dryrun-*.txt` 或 `report-live-*.txt`
- `latest_run_report.txt`

`latest_run_report.txt` 是最快速查看最近一次結果的檔案。

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

目前 repo 內有兩組核心 smoke tests：

```bash
conda run -n polymarket-bot python -m pytest -q tests/test_trade_manager.py tests/test_exit_fix.py
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

Important notes:

- Edit the tracked `.env` directly
- Default settings are currently optimization-oriented
- Logs and reports are written under `data/`
- The fastest post-run check is `data/latest_run_report.txt`
