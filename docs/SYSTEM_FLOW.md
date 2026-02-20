# Sanad Trader v3.0 — Complete System Flow

## Overview
Autonomous crypto trading system implementing a 6-layer verification pipeline with deterministic Python data plane and LLM intelligence layer.

---

## 🔄 COMPLETE END-TO-END FLOW

### PHASE 1: SIGNAL INGESTION (Every 5 minutes)

**Intelligence Sources (Parallel Execution):**

1. **CoinGecko Scanner** (`coingecko_client.py`)
   - Fetches trending coins + top gainers
   - Enriches with market cap, volume, price change
   - Saves to `signals/coingecko/YYYY-MM-DD_HH-MM.json`
   - Canonical source: `coingecko:trending` / `coingecko:trending_gainer`

2. **DexScreener Client** (`dexscreener_client.py`)
   - Scans Solana DEX pairs
   - Filters by liquidity, volume, price change
   - Saves to `signals/dexscreener/YYYY-MM-DD_HH-MM.json`
   - Canonical source: `dexscreener:boost`

3. **Birdeye Scanner** (`birdeye_client.py`)
   - Trending tokens on Solana
   - Market data: price, volume, liquidity, FDV
   - Saves to `signals/birdeye/YYYY-MM-DD_HH-MM.json`
   - Canonical source: `birdeye:trending`

4. **Birdeye DEX Scanner** (`dex_scanner.py`)
   - Additional Solana discovery via Birdeye trending + volume endpoints
   - 24h volume filter (min $100k)
   - Canonical source: `birdeye:dex_volume`

5. **Whale Tracker** (`whale_tracker.py`)
   - Monitors 21 seed wallets via Helius Enhanced Transactions API
   - Detects significant buys (>$10k)
   - Enriches with token metadata from Birdeye/Solscan
   - Saves to `signals/onchain/whale_TIMESTAMP.json`
   - Canonical source: `onchain:whale_alert`

6. **Majors Scanner** (`majors_scanner.py`)
   - Technical analysis for BTC/ETH/SOL (CEX majors)
   - 3 strategies: mean-reversion, trend-following, scalping
   - Uses Binance klines (1h, 4h, 1d candles)
   - Canonical source: `binance:ta_signal`

7. **Sentiment Scanner** (`sentiment_scanner.py`)
   - Twitter/Reddit mentions (placeholder)
   - Social sentiment scoring
   - Canonical source: `social:sentiment`

8. **Telegram Sniffer** (`telegram_sniffer.py`)
   - Monitors crypto alpha channels
   - Extracts token mentions
   - Canonical source: `telegram:alpha`

9. **On-Chain Analytics** (`onchain_analytics.py`)
   - Holder distribution analysis
   - Smart money flow detection
   - Saves heartbeat to `signals/onchain/_heartbeat.json`

**Supporting Intelligence:**

10. **Whale Discovery v2** (`whale_discovery_v2.py`)
    - Autonomous wallet expansion (every 2 hours)
    - 4 discovery modes: front-runners, graph expansion, regime labels, exploration
    - Max 100 wallets, performance-based promotion/demotion

11. **Fear & Greed Index** (`fear_greed_client.py`)
    - Daily market regime indicator
    - Saves to `signals/market/fear_greed_latest.json`

---

### PHASE 2: SIGNAL ROUTING (Every 10 minutes)

**Router** (`signal_router.py`) — **Deterministic Python, No LLMs**

**Step 1: Load Signals**
- Reads all signal files from last 30 minutes
- Checks staleness (warns if >30min old)
- Loads open positions from `state/positions.json`

**Step 2: Normalize Signals**
- Converts all signals to canonical schema via `signal_normalizer.py`
- Unified field names: `price_usd`, `volume_24h_usd`, `price_change_24h_pct`
- Stores canonical source key (e.g., `coingecko:trending`)

**Step 3: Corroboration Engine** (`corroboration_engine.py`)
- Cross-references signals by symbol/contract
- Assigns Sanad grade:
  - **MUTAWATIR** (3+ sources): Highest confidence
  - **TAWATUR_QAWIY** (2 sources, ≥1 EVIDENCE): Strong
  - **MASHHUR** (2 sources): Medium
  - **KHABAR_WAHID** (1 source): Weak
- Requires ≥1 EVIDENCE source (not just HYPE) for Tawatur

**Step 4: Filter Open Positions**
- Skips signals for tokens already in portfolio
- Checks available position slots (max 5 live, 10 paper)

**Step 5: Strategy Matching** (`strategy_registry.py`)
- Matches signals against 9 active strategies:
  - meme-momentum (72h hold, 5% SL, 20% TP)
  - whale-following (168h, 8% SL, 30% TP)
  - mean-reversion (48h, 3% SL, 10% TP)
  - trend-following (120h, 6% SL, 25% TP)
  - scalping (24h, 2% SL, 5% TP)
  - macro-trend (168h, 8% SL, 30% TP)
  - alt-breakout (120h, 6% SL, 25% TP)
  - dex-new-launch (72h, 5% SL, 20% TP)
  - cross-feed-validator (96h, 4% SL, 15% TP)

**Step 6: Market Regime Classification** (`regime_classifier.py`)
- Reads Fear & Greed Index
- Classifies: BULL / BEAR / UNKNOWN
- Adjusts tradeability thresholds dynamically

**Step 7: Tradeability Scoring** (`tradeability_scorer.py`)
- **6 components:**
  1. **Momentum** (35%): Price change strength + relative strength bonus
  2. **Volume** (20%): 24h volume vs chain-specific thresholds
  3. **Liquidity** (15%): Market depth, order book health
  4. **Timing** (15%): Freshness, volume trend
  5. **Catalyst** (10%): News, whale activity
  6. **Crowding** (5%): Retail vs smart money ratio
- **Regime-adaptive thresholds:**
  - BULL: 55 (selective)
  - UNKNOWN: 55 (conservative)
  - BEAR: 35 (opportunistic)
- Scores only top 20 candidates (performance optimization)

**Step 8: Ranking**
- Combines: base score + corroboration bonus + tradeability
- Sorts descending

**Step 9: Batch Selection**
- Takes top 2 signals per cycle (prevents timeout)
- Checks:
  - RugCheck.xyz score (blocks <50)
  - Skip list (toxic tokens)
  - Daily pipeline run limit (200 paper, 50 live)
  - Cooldown (30 min since last trade)

**Step 10: Enrichment**
- **Binance majors**: 24h ticker, klines, order book via `market_data_enricher.py`
- **Solana tokens**: Birdeye price/volume + Solscan holder data via `solscan_client.py`

**Step 11: Submit to Pipeline**
- Writes enriched signal to temp file
- Calls `sanad_pipeline.py` with 5-minute subprocess timeout
- Records counterfactual (rejected signals) to `state/counterfactual_log.json`

---

### PHASE 3: SANAD VERIFICATION PIPELINE (Per Signal)

**Pipeline** (`sanad_pipeline.py`) — **16 Gates, Fail-Closed**

#### Gate 0: Circuit Breakers (`policy_engine.py`)
- Checks kill switch, flash crash detector, rate limits
- **BLOCK** if any breaker is open

#### Gate 1: Kill Switch
- Manual emergency stop
- **BLOCK** if active

#### Gate 2: Daily P&L Check
- Reads portfolio daily profit/loss and drawdown
- **BLOCK** if over limits

#### Gate 3: Data Freshness
- Ensures price data <5 minutes old
- **BLOCK** if stale

#### Gate 4: Token Age
- Minimum 1 hour since token creation
- **BLOCK** if too new (prevents rug launch snipes)

#### Gate 5: Rugpull Scanner (`rugpull_scanner.py`)
- Checks:
  - Liquidity lock status
  - Holder distribution (top 10 <50%)
  - Honeypot detection via RugCheck.xyz
  - Contract verification
  - Ownership renounced
- **BLOCK** if >2 red flags

#### Gate 6: DEX Slippage Check (`dex_shadow.py`)
- Simulates trade on Raydium/Orca
- Checks slippage <3% (paper mode skips simulation)
- **BLOCK** if excessive slippage

#### Gate 7: CEX Spread Check
- Order book analysis for CEX trades
- Bid-ask spread <1%
- **BLOCK** if illiquid

#### Gate 8: Pre-Flight Simulation (`exchange_router.py`)
- Paper mode: skips on-chain simulation
- Live mode: tests trade execution path
- **BLOCK** if simulation fails

#### Gate 9: Price Stability
- 30-minute price volatility check
- **BLOCK** if >10% swing (pump/dump risk)

#### Gate 10: Exchange Health (`binance_client.py`, `mexc_client.py`)
- Checks exchange API status
- Error rate <5%
- **BLOCK** if exchange unstable

#### Gate 11: Reconciliation (`reconciliation.py`)
- Verifies state consistency between:
  - `positions.json`
  - `portfolio.json`
  - `trade_history.json`
- **BLOCK** if state mismatch or reconciliation stale (>10 min)

#### Gate 12: Exposure Limits (`policy_engine.py`)
- Per-token exposure: <10%
- Per-tier exposure:
  - Meme: <30%
  - Micro: <20%
  - Alt: <50%
- Open positions: <5 (live), <10 (paper)
- **BLOCK** if over limits

#### Gate 13: Cooldown Check
- 30-minute cooldown per token
- **BLOCK** if traded recently

#### Gate 14: Budget Check (`cost_tracker.py`)
- Daily API cost limit: $65 (paper), $25 (live)
- Monthly limit: $300
- **BLOCK** if budget exceeded

#### Gate 15: **SANAD TAKHRIJ** (Provenance Verification)

**Sanad Agent** (Claude Opus 4.6 in paper, Haiku in live for cost)

**Inputs:**
- Signal data (price, volume, source)
- Corroboration grade
- Market regime
- TokenProfile (on-chain metadata)

**Tasks:**
1. **Source Verification:**
   - Reads UCB1 source grades from `state/source_ucb1.json`
   - Weights sources by historical performance
   - Canonical source key lookup

2. **Corroboration Quality Check:**
   - Validates cross-source agreement
   - EVIDENCE sources > HYPE sources

3. **Sybil Detection:**
   - Checks for coordinated pump signals
   - Multiple low-quality sources = suspicious

4. **Rugpull Flags:**
   - Re-validates gate 5 findings
   - Adds LLM judgment on risk

5. **Confidence Score (0-100):**
   - MUTAWATIR + high UCB1 grade = 80-100
   - TAWATUR_QAWIY = 60-80
   - MASHHUR = 40-60
   - KHABAR_WAHID = 20-40

**Output:**
- **APPROVE** if confidence ≥45 and no rugpull flags
- **BLOCK** if confidence <45 or rugpull detected
- **REVISE** if needs more data (treated as BLOCK)

**Logging:**
- Writes Sanad report to temp file
- Records in DecisionPacket

---

#### Gates 16-19: STRATEGY, BULL/BEAR DEBATE, JUDGE (Only if Sanad APPROVE)

#### Gate 16: Strategy Matching

**Thompson Sampler** (`thompson_sampler.py`)
- Reads strategy performance from `state/strategy_performance.json`
- Bayesian sampling: explores underperforming strategies, exploits winners
- Selects best-fit strategy from registry

**Kelly Criterion** (`kelly_criterion.py`)
- Calculates position size based on:
  - Win rate
  - Win/loss ratio
  - Portfolio equity
  - Risk tolerance
- Fractional Kelly (0.25x) for safety

**Output:**
- Strategy name, position size ($), stop-loss, take-profit, max hold time

---

#### Gate 17-18: BULL/BEAR ADVERSARIAL DEBATE

**Al-Baqarah (Bull Agent)** — Claude Opus 4.6 (paper Haiku)
- Argues FOR the trade
- Bullish scenario: price catalysts, momentum, upside
- Researches via Perplexity Sonar Pro (web search)
- Estimates timeframe (e.g., "4-6 hours", "2-3 days")

**Al-Dahhak (Bear Agent)** — Claude Opus 4.6 (paper Haiku)
- Argues AGAINST the trade
- Bearish scenario: risks, red flags, downside
- Researches via Perplexity Sonar Pro
- Challenges Bull's assumptions

**Debate Duration:**
- ~60-90 seconds total
- Each agent gets full context + opponent's argument

---

#### Gate 19: **AL-MUHASBI JUDGE** (Final Arbiter)

**Judge Agent** — GPT-5.2-pro via `/v1/responses` API

**Inputs:**
- Full DecisionPacket:
  - All 15 gate results
  - Sanad confidence score
  - Strategy recommendation
  - Bull argument
  - Bear argument
  - TokenProfile
  - Market regime

**Tasks:**
1. **Bias Detection:**
   - Checks for confirmation bias in Bull/Bear
   - Flags overly optimistic/pessimistic reasoning

2. **Logic Errors:**
   - Identifies flawed assumptions
   - Validates Sanad methodology

3. **Risk Assessment:**
   - Compares risk/reward ratio
   - Checks downside protection (stop-loss)

4. **Final Decision:**
   - **APPROVE**: Trade proceeds to execution
   - **REJECT**: Trade blocked with detailed reason
   - **REVISE**: Needs more analysis (treated as REJECT)

**Output:**
- Judgment text (why approved/rejected)
- Confidence level (HIGH/MEDIUM/LOW)
- Writes full DecisionPacket to `genius-memory/decisions/`

---

### PHASE 4: EXECUTION (If APPROVE)

**Execution Layer** (`exchange_router.py`) — Claude Haiku 4.5

**Step 1: Exchange Selection**
- CEX trades → Binance or MEXC (based on pair availability)
- DEX trades → Raydium (Solana) via Jito MEV bundles

**Step 2: Order Placement**
- **Paper Mode**: Simulated execution
  - Records entry price from signal
  - Updates `positions.json`, `portfolio.json`, `trade_history.json`
  - No real money moved

- **Live Mode**: Real execution
  - CEX: Market order via exchange API
  - DEX: Burner wallet + Jito bundle (`burner_wallets.py`)
  - Checks partial fill (rejects if <90% filled)

**Step 3: Order Management System** (`oms.py`)
- Tracks order lifecycle: PENDING → FILLED / REJECTED / CANCELLED
- Writes to `state/orders.json`

**Step 4: Reconciliation**
- Updates portfolio equity
- Records trade in history
- Increments daily pipeline run counter

**Step 5: Notification** (`notifier.py`)
- Sends Telegram alert to user
- Format: "🟢 BUY: SOL @ $82.39 | Size: $140 | Strategy: whale-following"

---

### PHASE 5: POSITION MONITORING (Every 1 minute)

**Position Monitor** (`position_monitor.py`)

**Step 1: Load Open Positions**
- Reads `state/positions.json`
- Filters for status == "OPEN"

**Step 2: Price Updates**
- Fetches current prices via:
  - Binance API (CEX majors)
  - Birdeye API (Solana tokens)

**Step 3: P&L Calculation**
- Unrealized P&L = (current_price - entry_price) / entry_price
- Portfolio equity = cash + sum(unrealized P&L)

**Step 4: Exit Trigger Checks**

1. **Stop-Loss Check**
   - If price < stop_loss_price → EXIT (loss mitigation)

2. **Take-Profit Check**
   - If price > take_profit_price → EXIT (profit lock)

3. **Trailing Stop Check**
   - Adjusts stop upward as price rises
   - Protects gains while allowing upside

4. **Time-Based Exit** (`exit_time_parser.py`)
   - Parses Bull's timeframe (e.g., "4-6 hours")
   - If hold_time > max_hold_hours → EXIT (prevent overhold)

5. **Whale Exit Trigger** (`whale_exit_trigger.py`)
   - Monitors tracked wallets for SELLS
   - If whale dumps >20% position → EXIT (follow smart money)

6. **Sentiment Exit Trigger** (`sentiment_exit_trigger.py`)
   - Checks social sentiment shifts
   - If sentiment turns bearish → EXIT (prevent drawdown)

**Step 5: Execute Exit**
- Calls `exchange_router.py` with action="SELL"
- Updates positions, portfolio, trade history
- Records exit reason (e.g., "STOP_LOSS", "WHALE_EXIT")

**Step 6: Telegram Notification**
- Format: "🔴 SELL: SOL @ $81.89 | P&L: -0.61% | Reason: STOP_LOSS"

---

### PHASE 6: LEARNING LOOP (Continuous)

**Post-Trade Analyzer** (`post_trade_analyzer.py`) — Every 10 minutes

**Step 1: Load Closed Trades**
- Reads `state/trade_history.json`
- Filters for trades not yet analyzed

**Step 2: GPT-5.2 Analysis**
- Evaluates:
  - Why trade succeeded/failed
  - Sanad accuracy (was confidence justified?)
  - Strategy effectiveness
  - Exit timing quality

**Step 3: Update UCB1 Source Grades** (`ucb1_scorer.py`)
- Tracks performance by canonical source key
- Updates `state/source_ucb1.json`:
  - `pulls`: times selected
  - `reward_sum`: cumulative profit
  - `ucb1_score`: confidence + exploration bonus

**Step 4: Update Strategy Performance** (`strategy_registry.py`)
- Win rate, avg profit, max drawdown per strategy
- Writes to `state/strategy_performance.json`

**Step 5: Pattern Extraction** (`pattern_extractor.py`) — Every 6 hours
- LLM-powered analysis of winning trades
- Extracts recurring patterns
- Writes to `genius-memory/patterns/`

**Step 6: Vector DB Indexing** (`vector_db.py`)
- Stores trade reports, patterns, postmortems in ChromaDB
- Enables semantic search for future Sanad lookups

---

### PHASE 7: SELF-HEALING & MONITORING

**Heartbeat Monitor** (`heartbeat.py`) — Every 10 minutes
- Checks:
  - Kill switch status
  - Open positions (stop-loss violations)
  - Exposure limits
  - Flash crash detection (>15% drop in 5 min)
  - Cron job health
  - Circuit breakers
- Sends hourly Telegram summary (3 open positions, P&L)

**Watchdog** (`watchdog.py`) — Every 2 minutes
- 4-tier adaptive escalation:
  1. **Tier 0**: Log only (low severity)
  2. **Tier 1**: Auto-fix (refresh reconciliation)
  3. **Tier 2**: Telegram alert
  4. **Tier 3**: Human escalation
- Monitors:
  - Stale onchain signals (>30 min)
  - Reconciliation staleness
  - Router hangs (runningAtMs stuck)
  - Daily cost overrun
- Writes actions to `genius-memory/watchdog-actions/actions.jsonl`

**Reconciliation** (`reconciliation.py`) — Every 10 minutes
- Cross-checks state files for consistency
- Fixes drift between positions/portfolio/history
- Updates `state/cron_health.json` timestamp

---

### PHASE 8: PERIODIC MAINTENANCE

**Daily (00:00 UTC):**
- **Daily P&L Reset** (`daily_pnl_reset.py`): Resets daily profit counter
- **Daily Report** (`daily_report.py`): Summarizes trades, P&L, costs
- **Fear & Greed Update** (`fear_greed_client.py`): Fetches market regime
- **Rugpull DB Update** (`rugpull_db.py`): Refreshes scam token database

**Every 6 Hours:**
- **Pattern Extractor**: Analyzes winning patterns
- **UCB1 Grade Adapter** (`ucb1_grade_adapter.py`): Rebalances source weights
- **Exit Quality Analyzer** (`exit_quality_analyzer.py`): Evaluates exit timing
- **Counterfactual Tracker** (`counterfactual_tracker.py`): Tracks "what if" rejected signals

**Weekly (Sunday):**
- **Weekly Analysis** (`weekly_analysis.py`): Performance review
- **Weekly Research** (`weekly_research.py`): Market trend analysis
- **Whale Discovery Validation** (`whale_discovery.py --validate`): Prunes dead wallets
- **Red Team Audit** (`red_team.py`): GPT-5.2 security audit
- **Prompt Optimizer** (`prompt_optimizer.py`): Refines LLM prompts
- **Dust Sweeper** (`dust_sweeper.py`): Cleans up small positions

**Monthly:**
- **Model Check** (`model_check.py`): Validates LLM API keys
- **Security Audit** (`security_audit.py`): Full system penetration test

---

## 🛡️ SAFETY GUARANTEES

### Fail-Closed Architecture
- **ANY gate failure → BLOCK entire trade**
- No execution without: Sanad AND Judge AND Policy Engine PASS

### Deterministic Data Plane
- All signal processing, routing, scoring = pure Python
- No LLMs in critical path (only verification layer)

### Circuit Breakers (`state/circuit_breakers.json`)
- `kill_switch`: Manual emergency stop
- `flash_crash`: Auto-triggers on >15% BTC drop
- `daily_loss_limit`: Stops trading if -5% daily loss
- `max_drawdown`: Halts if -10% from peak

### Reconciliation Engine
- Every 10 minutes: verifies state consistency
- Detects phantom positions, double-counting, drift
- Auto-fixes minor issues, escalates critical ones

### Watchdog Auto-Response
- Stale data → refresh
- Stuck router → process kill
- Cost overrun → alert + reduce frequency
- Human escalation after 3 failed auto-fixes

---

## 📊 DATA FLOW SUMMARY

```
SIGNALS → Router → Pipeline (16 gates) → Judge → Execution → Position Monitor → Learning Loop
   ↓         ↓           ↓                   ↓          ↓              ↓              ↓
CoinGecko  Ranking    Sanad Takhrij       APPROVE    OMS          Exit Checks    UCB1 Update
DexScreener Filter    Bull/Bear          REJECT    Orders       Stop/TP/Time    Strategy Perf
Birdeye    Batch     Al-Muhasbi         REVISE    Paper/Live   Whale/Sentiment  Pattern Extract
WhaleTrack Enrich    Policy Engine                 Reconcile    Trailing Stop    Vector DB
Majors TA  Score     RugCheck                      Notify       P&L Track        Postmortem
Telegram   Strategy  Exchange Health
OnChain    Corr.     Exposure Limits
```

---

## 🔑 KEY STATE FILES

| File | Purpose | Update Frequency |
|------|---------|------------------|
| `state/positions.json` | Open positions | Real-time |
| `state/portfolio.json` | Cash, equity, mode | Real-time |
| `state/trade_history.json` | Closed trades | Per trade |
| `state/source_ucb1.json` | Source performance grades | Per closed trade |
| `state/strategy_performance.json` | Strategy win rates | Per closed trade |
| `state/circuit_breakers.json` | Safety switches | Real-time |
| `state/cron_health.json` | Job timestamps | Per job run |
| `state/signal_router_state.json` | Router metadata | Per cycle |
| `state/counterfactual_log.json` | Rejected signals | Per router run |
| `state/skip_tokens.json` | Toxic token blocklist | Manual + watchdog |
| `genius-memory/decisions/` | DecisionPackets | Per pipeline run |
| `genius-memory/patterns/` | Winning patterns | Every 6 hours |
| `genius-memory/watchdog-actions/` | Auto-fix logs | Per watchdog run |

---

## 🎯 CURRENT STATUS

**System Mode**: PAPER (simulated trading)
**Open Positions**: 3 (SOL, ETH, BTC)
**Portfolio Equity**: $9,939.06 (-0.66% drawdown)
**Router Stability**: 6+ consecutive successful runs
**Learning Loop**: Active (18 trades analyzed, 9 sources tracked)
**Safety**: All circuit breakers closed, no escalations

**Next Milestone**: Promote to LIVE mode after sustained 48-hour paper trading stability.

---

*Last Updated: 2026-02-20 07:53 UTC*
*System Version: v3.0*
*Architecture: 6-Layer Sanad Trust Framework*
