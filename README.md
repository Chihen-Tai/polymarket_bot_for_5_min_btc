# polymarket-bot-by_openclaw

A high-frequency, event-driven trading bot designed specifically for **Polymarket 5-minute BTC markets (btc-updown-5m)**. 

---

## 🛡️ VPN-Safe Conservative Mode (Recommended for Live)

本機器人現在預設啟動 **VPN-safe, maker-first, expiry-first** 模式。這是專為從亞洲透過 VPN 交易美國 CLOB 等高延遲環境所設計的。

### 核心原則 (Core Principles)
- **Maker-First (掛單優先)**：預設僅使用掛單 (Limit Order)，不主動追價。`VPN_MAKER_ONLY=True` 會禁用所有 Taker 備案。
- **Expiry-First (持有至結算)**：大部分倉位將持有至 5 分鐘結束自動結算，以賺取完整的 Time Decay 並避免在流動性不足時主動出場帶來的滑價與手續費。
- **Entry Window Guard (進場窗口保護)**：嚴格禁止在 `secs_left < 150` 時進場，避免後段波動與執行風險。
- **Latency Monitoring (延遲監控)**：持續追蹤 E2E 延遲 (p50, p95, Jitter)。若網路環境惡化，機器人會自動暫停新進場。
- **Executable Edge (實際期望值)**：進場要求至少 0.06 的預期報酬率 (Edge Floor)，已涵蓋手續費、價差與延遲緩衝。

---

## 🇹🇼 中文說明 (Chinese Documentation)

這個機器人專注於高頻率的 5 分鐘二元期權市場。請注意，在 **VPN Safe Mode** 下，許多高頻狙擊策略（如 `ws_flash_snipe`, `strike_cross_snipe`）會被自動禁用，改以穩健的中長線預測為主。

### 🚀 核心策略 (Core Strategies)
*   **WS Flash Snipe (常規動能狙擊)**：監控幣安的資金流與報價陡升。 (VPN 模式下預設禁用)
*   **Early Underdog Sniper (開局逆勢樂透)**：專注於開局 4 分鐘內的市場。 (VPN Live 模式下預設禁用)
*   **Conservative Structural Entry**：在 VPN 模式下，機器人僅保留較慢、信度較高的結構性策略。

### 🛡️ 極致風控與結算 (Risk & Ev-Optimization)
為了在 VPN 環境下生存，風控逻辑已大幅簡化：
1.  **Expiry First (預設持有至結算)**：除非觸發硬停損 (Hard Stop)，否則預設持有至 5 分鐘結束，不進行中途主動停利。
2.  **Maker Only (掛單進場)**：僅在訂單簿有足夠深度且能以 Maker 價格成交時進場。
3.  **Latency Block (延遲阻斷)**：若網路抖動 (Jitter) 超過 250ms 或平均延遲超過 600ms，自動停止交易。

### 📦 安裝與啟動
建議使用 Conda 建立純淨環境：
```bash
conda env create -f environment.yml
conda activate polymarket-bot
```

**啟動 VPN-Safe 模式（預設）**：
確保 `.env` 中 `VPN_SAFE_MODE=True`。

### 📊 設定與報表
執行 `python scripts/journal_analysis.py` 查看詳細報表，重點關注：
- **Actual vs Observed Gap**：實際成交與模型理論值的差距。
- **Timing Buckets**：不同時段進場的表現（240s+, 180s+, <150s）。
- **Fee-Adjusted Actual PnL**：扣除所有手續費後的真實淨利。

---

## 🇬🇧 English Documentation (Simplified)

This bot is now optimized for **VPN-safe** environments. It prioritizes limit orders (Maker-first) and holding until market expiry (Expiry-first).

### 🚀 Key Features
- **Latency Guard**: Blocks trades if E2E jitter > 250ms or p95 > 600ms.
- **Strict Entry Window**: No new entries allowed in the final 150 seconds of the 5m window.
- **Maker-Only Execution**: Reduces fee drag and slippage by avoiding market orders.
- **Actual-Aware PnL**: Reports focus on real, fee-adjusted USDC returns, not theoretical prices.

### 📊 Performance Analysis
Run `python scripts/journal_analysis.py` to get a breakdown by:
- **Timing Buckets**: (240-300s, 180-240s, 150-180s, <150s).
- **Execution Style**: (Maker vs Taker vs Expiry).
- **Actual PnL**: Real USDC growth after all execution costs.
