# polymarket-bot-by_openclaw
[English Version Below / 英文版附於文末↓](#english-version)

Polymarket 自動量化交易機器人 (專攻 5 分鐘比特幣市場)

## 🎯 核心特色機制
- **高頻事件驅動架構 (Event-Driven Sniper)**：透過專屬 WebSocket 背景長連線 (Daemon) 實時訂閱幣安 `@aggTrade` 與 `@bookTicker`。
- **訂單流失衡與微秒級中斷 (OFI & CVD)**：毫秒級解析全網買賣動能，一旦失衡立刻「打斷睡眠週期」光速交易，徹底消除傳統 REST API 固定輪詢產生的「逆向選擇 (Adverse Selection)」風險。
- **ZLSMA + 枝形吊燈停損 (Chandelier Exit)**：內建高效能趨勢捕捉指標，過濾盤整雜訊。
- **凱利公式注碼控制 (Quarter Kelly Sizing)**：根據策略歷史勝率動態決定下注金額。
- **每日熔斷系統 (Daily Circuit Breaker)**：當日虧損達標自動關機，拒絕攤平。
- **階梯停利 (Principal Extraction)**：暴漲時自動抽離本金，留下無風險彩票 (Risk-Free Moonbag) 繼續奔跑。

## 🚀 快速開始

### 1. 環境安裝
```bash
git clone https://github.com/Chihen-Tai/polymarket-bot-by_openclaw.git
cd polymarket-bot-by_openclaw
conda env create -f environment.yml
conda activate polymarket-bot
cp .env.example .env
```

### 2. 環境變數設定 (`.env`)
實盤交易前必填：
- `PRIVATE_KEY`：你的錢包私鑰
- `FUNDER_ADDRESS`：你的錢包地址
- `DRY_RUN=True`：強烈建議先開紙上模擬，True 為不動用真金白銀。
- `AUTO_MARKET_SELECTION=True`：讓系統自動每 5 分鐘抓取最新的 BTC 市場。

### 3. 啟動機器人
所有的實盤與模擬交易整合至單一主程式：
```bash
python main.py
```

### 4. 結算與對帳分析
按下 `Ctrl+C` 終止主程式後，系統會自動印出當次成績單。若需手動匯出報表：
```bash
# 確保在 conda 環境內執行 (conda activate polymarket-bot)
python scripts/verify_close_accounting.py --limit 50 --format json --output data/close_accounting.json
python scripts/trade_pair_ledger.py --limit 50 --format csv --output data/trade_ledger.csv
```

---

<br><br>

<a name="english-version"></a>
# English Version

Polymarket Automated Quantitative Trading Bot (Optimized for 5-Minute BTC Markets)

## 🎯 Core Features
- **High-Frequency Event-Driven Architecture**: Employs a dedicated background WebSocket daemon to stream live Binance `@aggTrade` and `@bookTicker` data.
- **OFI & CVD Interrupt Sniping**: Computes real-time Order Flow Imbalance. Instantly interrupts sleep cycles to snipe Polymarket contracts upon severe momentum shifts, eliminating Adverse Selection latency.
- **ZLSMA + Chandelier Exit**: Built-in high-performance trend-following indicators to filter ranging market noise.
- **Quarter Kelly Position Sizing**: Dynamically adjusts bet size based on the historical win rate of the strategy.
- **Daily Circuit Breaker**: Automatically halts trading when the maximum daily drawdown limit is hit, preventing revenge trading.
- **Tiered Take-Profit (Principal Extraction)**: Extracts initial capital upon sudden profit surges, leaving a "Risk-Free Moonbag" to capture exponential upside without baseline risk.

## 🚀 Quick Start

### 1. Installation
```bash
git clone https://github.com/Chihen-Tai/polymarket-bot-by_openclaw.git
cd polymarket-bot-by_openclaw
conda env create -f environment.yml
conda activate polymarket-bot
cp .env.example .env
```

### 2. Environment Setup (`.env`)
Required before live trading:
- `PRIVATE_KEY`: Your wallet private key.
- `FUNDER_ADDRESS`: Your wallet public address.
- `DRY_RUN=True`: Highly recommended to start in paper-trading simulation mode.
- `AUTO_MARKET_SELECTION=True`: Enables the bot to autonomously cycle through upcoming 5m BTC markets.

### 3. Running the Bot
Launch the unified core engine:
```bash
python main.py
```

### 4. Accounting & Analysis
Terminating `main.py` via `Ctrl+C` will automatically print the run report. For manual exports:
```bash
# Export JSON/CSV ledger and analysis to data/ directory
python scripts/verify_close_accounting.py --limit 50 --format json --output data/close_accounting.json
python scripts/trade_pair_ledger.py --limit 50 --format csv --output data/trade_ledger.csv
```
