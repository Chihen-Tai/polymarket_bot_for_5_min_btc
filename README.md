# polymarket-bot-by_openclaw

Polymarket 自動交易機器人 但是目前我都虧錢

## 目標
- BTC 5min 策略骨架
- 嚴格風控（資產不得低於 $5）
- 先用 dry-run，不下真單

## 快速開始
```bash
cd polymarket-bot-by_openclaw
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python runner.py
```

查 token id（UP/DOWN）小工具：
```bash
source .venv/bin/activate
python tools_fetch_token_ids.py btc-updown-5m-1773691500
```

快速檢查出場規則：
```bash
source .venv/bin/activate
python test_trade_manager.py
python replay_exit_checks.py
python paper_replay_runner.py
python reconcile_journal.py
python inspect_trades.py --limit 10
python verify_close_accounting.py --limit 20 --summary
python trade_pair_ledger.py --limit 20 --summary
python ledger_summary.py --limit 50
```

報表 / 匯出：
```bash
source .venv/bin/activate
python verify_close_accounting.py --limit 50 --format json --output reports/close_accounting.json
python verify_close_accounting.py --limit 50 --format csv --output reports/close_accounting.csv
python trade_pair_ledger.py --limit 50 --format json --output reports/trade_ledger.json
python trade_pair_ledger.py --limit 50 --format csv --output reports/trade_ledger.csv --show-legs
python ledger_summary.py --limit 100 --format json --output reports/ledger_summary.json
```

actual source tier：
- `high`: cash balance delta（最高可信）
- `medium`: close response / response amount 類來源
- `low`: observed only、unavailable、或其他低可信推估

## 模式
- `DRY_RUN=true`：只模擬下單
- `DRY_RUN=false`：接真實 CLOB API（已接上）

## 風控（預設）
- `MIN_EQUITY=5.0`
- `MAX_ORDER_USD=1.0`
- `MAX_EXPOSURE_USD=3.0`
- `MAX_ORDERS_PER_5MIN=1`
- `MAX_CONSEC_LOSS=3`
- `DAILY_MAX_LOSS=2.0`

## 實盤啟用前必填
請在 `.env` 填入：
1. `PRIVATE_KEY`
2. `FUNDER_ADDRESS`

建議啟用：
- `AUTO_MARKET_SELECTION=true`
- `MARKET_SLUG_PREFIX=btc-updown-5m-`

這樣機器人會自動抓最新 BTC 5min 市場並切換 token ids，不需要每 5 分鐘手改。
