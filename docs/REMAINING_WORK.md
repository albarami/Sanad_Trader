# Remaining Work After Feb 20 Stability Sprint

## ✅ What Was Fixed (Verified Working)

1. **Data Pipeline** - commit 41273d2, 5e5f8e2, cc3d3b3
   - Field name unification (volume_24h_usd, price_change_24h_pct)
   - Binance enrichment for majors (real-time market data)
   - Normalization before signal_window registration
   - 117 stale signals purged

2. **Scoring Calibration** - commit 9afcb51, 8127e9b
   - Absolute volume thresholds ($1B+ = 20pts)
   - Momentum uses abs(price_change) for negative moves
   - Relative strength bonus (+5pts for trending while down)
   - XRP: 43 → 73/100 (exceeds 70 target)

3. **Router Stability** - commit f8a4079, 853203e, ecdbc4a
   - State updates at START (prevents false stall alerts)
   - Watchdog checks actual signal files (no false positives)
   - Process group kill enforces 5-min timeout (was 30-90min hangs)

4. **Learning Loop** - commit 762fc4e
   - Post-trade analyzer field name fix (timestamp/exit_time)
   - UCB1 tracking 9 sources (was 1)
   - 18 trades analyzed, 25 genius memory files created

5. **Configuration** - commit a24c1a6, 3747eb4, 910f0c7, 094d54d
   - Tradeability thresholds in config/thresholds.yaml
   - Canonical signal schema documented
   - Data source strategy documented
   - Whale seed list (13 named wallets from XLSX)

6. **API Optimization** - commit 8127e9b
   - Binance 60s cache (252ms → 88ms)
   - Prevents rate limit issues (168 calls/hour → ~20 unique/hour)

## ⚠️ What Needs Verification (Next 24-48h)

### 1. Router with Fresh Normalized Signals
**Status:** Should work, needs real-world test
- signal_window purged (117 stale → 0)
- Next scanner run will populate with normalized signals
- Verify: Check signal_window.json has volume_24h_usd (not None)

**How to verify:**
```bash
cd /data/.openclaw/workspace/trading
# Wait for next coingecko_monitor run (every 5min)
# Then check:
python3 -c "
import json
window = json.load(open('state/signal_window.json'))
xrp = [s for s in window['signals'] if s.get('token') == 'XRP'][0]
print(f'volume_24h_usd: {xrp.get(\"volume_24h_usd\")}')
print(f'chain: {xrp.get(\"chain\")}')
"
```
**Expected:** volume_24h_usd > 0, chain = "binance"

### 2. Whale Tracker with New Seed List
**Status:** Config synced, needs validation
- whale_wallets.json synced with 13 XLSX wallets
- whale_tracker.py reads from whale_wallets.json
- Whale discovery v2 has co-buyer/front-runner logic

**How to verify:**
```bash
# Check whale tracker loads correct wallets
grep -A 1 "Loading whale wallets" execution-logs/whale_tracker.log | tail -2

# Check discovery finds candidates
ls -lh state/candidate_whales.json
```
**Expected:** 13 wallets loaded, candidates file starts populating

### 3. Next Trade Execution
**Status:** Pipeline ready, waiting for qualifying signal
- Tradeability threshold: 40 (UNKNOWN regime)
- XRP scores 73 (would pass)
- Sanad/Bull/Bear/Judge all operational

**How to verify:**
```bash
# Watch for next signal passing gates
tail -f execution-logs/signal_router.log | grep "Calling pipeline"
```
**Expected:** Next strong signal → Sanad → Bull/Bear → Judge → Execute

### 4. Learning Loop Updates
**Status:** Fixed, needs next closed trade to confirm
- Post-trade analyzer now reads trade_history.json correctly
- UCB1 updates source grades after each trade
- Pattern extraction runs every 10 trades

**How to verify:**
```bash
# After next closed trade, check:
cat state/source_ucb1.json | jq '.sources | length'
# Should be 9+ sources
cat genius-memory/master-stats.md
# Should show updated win rate / total trades
```
**Expected:** Source grades adapt, genius memory accumulates

## 🔴 Critical Missing Pieces (Sprint 4)

### 1. Solana Token Enrichment
**Problem:** Solana signals score low (no real-time data)
**Root cause:** market_data_enricher.py has placeholder for Birdeye
**Impact:** Can't trade Solana tokens effectively

**Fix required:**
```python
# In market_data_enricher.py, replace _enrich_solana_token():
def _enrich_solana_token(signal: dict) -> dict:
    token_address = signal.get("token_address", "")
    if not token_address:
        return signal
    
    # Birdeye market data
    birdeye_data = birdeye_client.get_token_overview(token_address)
    signal["volume_24h_usd"] = birdeye_data.get("volume24h", 0)
    signal["price_change_1h_pct"] = birdeye_data.get("priceChange1h", 0)
    signal["price_change_24h_pct"] = birdeye_data.get("priceChange24h", 0)
    signal["liquidity_usd"] = birdeye_data.get("liquidity", 0)
    
    # Solscan holder data
    solscan_data = solscan_client.get_token_holders(token_address)
    signal["holder_count"] = solscan_data.get("total", 0)
    signal["top10_holder_pct"] = solscan_data.get("top10_pct", 0)
    
    return signal
```

**Priority:** HIGH (blocks 90% of onchain signals)

### 2. Wrapped SOL False Positives
**Problem:** Whale distribution alerts fire when whales spend SOL to buy
**Root cause:** whale_tracker detects SOL outflows as "distribution"
**Impact:** False alerts, noise in signal feed

**Fix required:**
```python
# In whale_tracker.py, filter SOL transfers with swap context:
if token_mint == "So11111111111111111111111111111111111111112":
    # Check if this SOL transfer is part of a swap
    if _is_swap_related(tx):
        continue  # Ignore SOL spent to buy tokens
```

**Priority:** MEDIUM (reduces noise, not blocking trades)

### 3. Volume Scoring Calibration
**Problem:** XRP $2.6B scores same as $100M token (both get 20/20)
**Root cause:** Hardcoded thresholds (≥$1B = max points)
**Impact:** Can't differentiate mega-caps from mid-caps

**Fix required:**
```python
# In tradeability_scorer.py, use logarithmic scale:
if vol_24h >= 1_000_000_000:  # ≥$1B
    volume_score = min(20, 10 + math.log10(vol_24h / 1e9) * 5)
    # $1B = 10pts, $10B = 15pts, $100B = 20pts
```

**Priority:** LOW (system works, just not optimal)

### 4. Fear & Greed Regime Detection
**Problem:** Regime stuck at UNKNOWN (no data source)
**Root cause:** fear_greed_index.py not implemented
**Impact:** Can't use regime-adaptive thresholds (BULL vs BEAR)

**Fix required:**
- Implement fear_greed_index.py using Alternative.me API
- Or use BTC price momentum as proxy (>200d MA = BULL)

**Priority:** MEDIUM (improves threshold adaptation)

## 📊 Monitoring Checklist (Daily)

1. **Router Health:**
   ```bash
   tail -20 execution-logs/signal_router.log | grep "Selected\|Tradeability\|ERROR"
   ```
   Expected: Signals scoring, no errors

2. **Watchdog Actions:**
   ```bash
   tail -10 genius-memory/watchdog-actions/actions.jsonl
   ```
   Expected: Tier 0/1 (info), not Tier 3/4 (critical)

3. **Learning Loop:**
   ```bash
   cat genius-memory/master-stats.md | grep "Total Trades\|Win Rate"
   ```
   Expected: Increasing trades, improving win rate

4. **Cost Tracking:**
   ```bash
   tail -5 state/cost_tracking.json | jq '.daily_cost'
   ```
   Expected: <$10/day (paper mode with Haiku)

5. **Signal Quality:**
   ```bash
   ls -lh signals/*/$(date +%Y-%m-%d)*.json | wc -l
   ```
   Expected: 10-50 signals/day across all sources

## 🎯 Success Metrics (Week 1 Post-Fix)

- [ ] 3+ trades executed (signal → Sanad → Bull/Bear → Judge → Execute)
- [ ] 0 router hangs (watchdog Tier 3+ escalations)
- [ ] Learning loop updates after each trade (UCB1 source count increases)
- [ ] Whale discovery adds 2+ candidates (co-buyer or front-runner)
- [ ] XRP scores 70-80 consistently (with fresh Binance data)
- [ ] Cost <$10/day (paper mode baseline)

## 🚫 Red Flags (Immediate Escalation)

1. **Router stuck >10min** → Check process group kill (commit ecdbc4a)
2. **0 signals for >1 hour** → Check scanner cron jobs
3. **Watchdog Tier 4 escalation** → Human intervention required
4. **Cost spike >$20/day** → Check for infinite loops
5. **signal_window still has volume_24h: None** → Normalization broken

## 📝 Notes

- **Thresholds:** Kept at UNKNOWN=40 (not lowered to force trades)
  - This is temporary until Solana enrichment is done
  - Once Solana tokens score correctly, raise to 50-55
  
- **Whale expansion:** Will be slow initially (needs real trades to discover)
  - Expected: 1-2 candidates/week early on
  - Accelerates once we have 20+ trades to mine for front-runners

- **Data source priority:**
  - Binance majors: Use Binance (authoritative for CEX)
  - Solana tokens: Use Birdeye/Solscan (authoritative for DEX)
  - CoinGecko: Discovery only (not execution data)
