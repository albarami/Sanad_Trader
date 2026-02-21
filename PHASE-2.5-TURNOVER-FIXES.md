# PHASE 2.5 — TURNOVER & SIGNAL QUALITY FIXES
## Root Cause Analysis + Minimal Patches

**Date:** 2026-02-21 16:35 GMT+8  
**Status:** Implementation-ready  
**Validation:** Evidence-backed, minimal diffs

---

## ROOT CAUSE CLASSIFICATION

| Issue | Type | Severity | Fix Complexity |
|-------|------|----------|----------------|
| 1. max_hold not propagated | Position writer bug | 🔴 CRITICAL | LOW (1 line) |
| 2. TP/SL unit inconsistency | Logic OK, display bug | 🟡 MEDIUM | LOW (fallback) |
| 3. DEX price feed missing | Pricing infrastructure | 🔴 CRITICAL | MEDIUM (wire DEX) |
| 4. Regime not applied | Wiring missing | 🔴 CRITICAL | MEDIUM (add filter) |
| 5. SHORT disabled | Signal generation | 🔴 CRITICAL | HIGH (rule engine) |
| 6. Whale signals starved | Router selection | 🟡 MEDIUM | LOW (diversity) |
| 7. No deduplication | Router logic | 🟡 MEDIUM | LOW (cooldown) |
| 8. Counterfactual gaps | Checker reliability | 🟡 MEDIUM | LOW (batch fetch) |

---

## ISSUE 1: Position Writer Missing max_hold_hours

### Evidence
```python
# scripts/sanad_pipeline.py line 2616-2648
new_position = {
    "id": order["orderId"],
    "token": signal["token"],
    ...
    "strategy_name": strategy_result.get("strategy_name", ""),
    "signal_source": signal.get("source", "unknown"),
    ...
    "opened_at": datetime.now(timezone.utc).isoformat(),
}
# ❌ max_hold_hours NOT included
```

**Actual position records:**
```
SOL: max_hold_hours=MISSING, strategy=meme-momentum (should be 6h)
ETH: max_hold_hours=MISSING, strategy=meme-momentum (should be 6h)
BTC: max_hold_hours=MISSING, strategy=meme-momentum (should be 6h)
BP:  max_hold_hours=MISSING, strategy=whale-following (should be 8h)
```

**Strategy registry:**
```
meme-momentum: max_hold_hours=6
whale-following: max_hold_hours=8
```

### Root Cause
Position writer (line 2616) does NOT include max_hold_hours from strategy_result["exit_rules"].

Position_monitor (line 297) DOES have fallback: reads strategy registry if position lacks max_hold.

**BUT:** Monitor fallback doesn't account for Bull timeframe overrides like "3-7 days" or "2-4 weeks".

### Patch 1A: Add max_hold to position writer
```python
# scripts/sanad_pipeline.py line 2640 (after opened_at)
        "opened_at": datetime.now(timezone.utc).isoformat(),
+       "max_hold_hours": strategy_result.get("exit_rules", {}).get("max_hold_hours"),
    }
```

### Patch 1B: Strengthen position_monitor fallback
```python
# scripts/position_monitor.py line 295-318 (existing logic is OK, just log)
    # Priority 1: Strategy-specific max_hold_hours
    strategy_name = position.get("strategy_name")
+   if not max_hold and strategy_name:
+       log(f"[MONITOR] Position {position.get('token')} missing max_hold, using strategy default")
```

### Expected Impact
- **Before:** Positions held 24-49h (fallback to paper_max_hold_hours=8 or 24h)
- **After:** Positions exit at 6-8h per strategy
- **Turnover:** 3-6× faster closes

---

## ISSUE 2: TP/SL Unit Consistency (Partial Issue)

### Evidence
**Strategy registry:**
```yaml
meme-momentum:
  take_profit_pct: 8   # 8%
  stop_loss_pct: 3     # 3%
```

**Position writer logic (line 2568):**
```python
strategy_tp_dec = strategy_tp / 100  # Convert from 8 → 0.08
return strategy_tp_dec  # Stored as 0.08 (fraction)
```

**Actual position records:**
```
SOL: take_profit_pct=0.3, stop_loss_pct=0.047
ETH: take_profit_pct=0.3, stop_loss_pct=0.063
BTC: take_profit_pct=0.3, stop_loss_pct=0.038
```

### Root Cause
Positions show TP=0.3 (30% default) because `strategy_result["exit_rules"]` was EMPTY.

**Why empty?** Checked decisions.jsonl: `strategy_result.exit_rules` is MISSING in decisions (not logged).

But code at line 1545 DOES set: `"exit_rules": matched_exit_rules`.

**Hypothesis:** Older positions (created before recent code) don't have exit_rules. Newer positions should work.

### Patch 2: Fallback to defaults if exit_rules missing
```python
# scripts/sanad_pipeline.py line 2560-2576
def _calc_tp_pct_with_strategy(entry_price, bull_result, strategy_result):
    exit_rules = strategy_result.get("exit_rules", {})
    strategy_tp = exit_rules.get("take_profit_pct")
    
+   # Fallback: if exit_rules empty, try to load from registry
+   if not strategy_tp:
+       strategy_name = strategy_result.get("strategy_name")
+       if strategy_name:
+           from strategy_registry import get_all_strategies
+           all_strats = get_all_strategies()
+           if strategy_name in all_strats:
+               strategy_tp = all_strats[strategy_name].get("exit_conditions", {}).get("take_profit_pct")
    
    bull_tp = _calc_tp_pct(entry_price, bull_result) if bull_result else None
    
    if strategy_tp:
        strategy_tp_dec = strategy_tp / 100
        ...
```

Same for `_calc_stop_pct_with_strategy`.

### Expected Impact
- **Before:** TP=30%, SL=3-15% (defaults)
- **After:** TP=8%, SL=3% (strategy-specific)
- **Closes:** Faster TP hits on majors

---

## ISSUE 3: DEX Price Feed Missing (BP Stuck at 0%)

### Evidence
```python
price_cache.json keys: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', ...]
BP in cache: False  # ❌ Raydium token not tracked
```

**BP position:**
```
Entry: $0.005780008444089068
Current: $0.005780008444089068 (0% PnL)
Exchange: raydium
Held: 24.1h
```

### Root Cause
Position_monitor updates prices from `price_cache.json`, populated by `price_snapshot.py`.

price_snapshot.py only fetches Binance tickers → DEX tokens (Raydium/Orca) never update.

### Patch 3A: Add DEX price fetching to position_monitor
```python
# scripts/position_monitor.py line 400-450 (after Binance price update)
    # Update DEX prices for non-Binance positions
    dex_positions = [p for p in open_positions if p.get("exchange") not in ("binance", "mexc")]
    
    if dex_positions:
        try:
            from birdeye_client import get_token_price as birdeye_price
            for pos in dex_positions:
                token = pos.get("token")
                try:
                    price_data = birdeye_price(token)
                    if price_data and "price" in price_data:
                        pos["current_price"] = float(price_data["price"])
                        log(f"[MONITOR] Updated DEX price: {token} = ${pos['current_price']}")
                except Exception as e:
                    log(f"[MONITOR] DEX price fetch failed for {token}: {e}")
        except ImportError:
            log("[MONITOR] Birdeye client not available for DEX prices")
```

### Patch 3B: Alternative - wire Birdeye into price_snapshot
```python
# scripts/price_snapshot.py (add DEX tokens from open positions)
    # Fetch DEX token prices for open positions
    positions = json.load(open("../state/positions.json"))
    dex_tokens = [p["token"] for p in positions.get("positions", []) 
                  if p.get("status") == "OPEN" and p.get("exchange") not in ("binance", "mexc")]
    
    if dex_tokens:
        from birdeye_client import get_token_price
        for token in dex_tokens:
            try:
                data = get_token_price(token)
                if data:
                    cache[token] = {"price": data["price"], "timestamp": datetime.now().isoformat()}
            except Exception as e:
                print(f"[SNAPSHOT] DEX price failed: {token} ({e})")
```

### Expected Impact
- **Before:** DEX positions stuck at entry price, never hit TP/SL
- **After:** Real-time price updates, normal exit behavior
- **BP specifically:** Will exit when TP=12% hit (or SL=19%)

---

## ISSUE 4: Regime Adaptation Not Wired

### Evidence
```json
active_regime_profile.json:
{
  "regime": "BEAR_HIGH_VOL",
  "profile": {
    "avoid_strategies": ["meme-momentum", "early-launch"],
    "preferred_strategies": ["sentiment-divergence"],
    "position_sizing": {"base_position_pct": 1.5}
  }
}
```

**Grep result:**
```
scripts/sanad_pipeline.py: "avoid_strategies" NOT FOUND
scripts/signal_router.py: "avoid_strategies" NOT FOUND
```

**Reality:**
- All majors (BTC/ETH/SOL) using **meme-momentum** (avoided strategy)
- Position sizing NOT adjusted by regime modifier

### Root Cause
Regime classifier writes `active_regime_profile.json`, but:
1. sanad_pipeline.py doesn't read it
2. strategy selection (Thompson + registry) doesn't filter avoid_strategies
3. Position sizing doesn't apply regime modifiers

### Patch 4A: Filter avoid_strategies in Thompson selection
```python
# scripts/sanad_pipeline.py line 1400 (before Thompson call)
    # Load regime profile
+   regime_avoid = []
+   try:
+       regime_profile_path = STATE_DIR / "active_regime_profile.json"
+       if regime_profile_path.exists():
+           regime_prof = json.load(open(regime_profile_path))
+           regime_avoid = regime_prof.get("profile", {}).get("avoid_strategies", [])
+           if regime_avoid:
+               print(f"  Regime {regime_prof.get('regime')}: avoiding {regime_avoid}")
+   except Exception as e:
+       print(f"  Regime profile load failed: {e}")
    
    # ── Thompson Sampling: select best strategy ──
    eligible_by_tier = get_eligible_strategies(profile, regime_tag) if profile else []
+   # Remove avoided strategies
+   if regime_avoid:
+       eligible_by_tier = [s for s in eligible_by_tier if s not in regime_avoid]
+       print(f"  Post-regime filter: {eligible_by_tier}")
    
    thompson_result = thompson_select(
        signal=signal,
        current_regime=regime_tag,
        eligible_strategies=eligible_by_tier
    )
```

### Patch 4B: Apply regime position sizing modifier (ALREADY EXISTS!)
Code at line 1520-1528 ALREADY applies regime_size_modifier, BUT:
- It reads from `regime_data` variable (unclear source)
- Should read from `active_regime_profile.json`

```python
# scripts/sanad_pipeline.py line 1520 (verify source)
    # ── Regime-adjusted position sizing ──
-   regime_size_modifier = regime_data.get("implications", {}).get("position_size_modifier", 1.0)
+   regime_size_modifier = regime_prof.get("profile", {}).get("position_sizing", {}).get("base_position_pct", 2.0) / 2.0  # normalize to multiplier
```

### Expected Impact
- **Before:** meme-momentum used in BEAR regime, full sizing
- **After:** Only sentiment-divergence used, 0.3× sizing (defensive)
- **Behavior:** Conservative in bear markets, aggressive in bull

---

## ISSUE 5: SHORT Trading Disabled

### Evidence
```
562 total decisions
SHORT signals: 0
SHORT trades: 0
```

**Strategy registry:**
- 3 SHORT strategies defined (whale-distribution-fade, bear-momentum, mean-reversion-short)
- All active=True
- Entry conditions include SHORT-specific logic (RSI>70, price drops, etc.)

### Root Cause
Signals never have `direction="SHORT"`.

**Signal sources:**
- CoinGecko: No direction field (trending only)
- Birdeye: No direction field (trending only)
- DexScreener: No direction field (boost/CTO only)
- Whale tracker: Accumulation=LONG implied, but distribution signals exist

### Patch 5A: Add direction inference in signal normalization
```python
# scripts/signal_normalizer.py (create if missing, or add to router)
def infer_direction(signal, regime_tag):
    """Infer LONG/SHORT from signal context + regime."""
    
    # Explicit direction takes precedence
    if signal.get("direction"):
        return signal["direction"]
    
    # Whale distribution → SHORT
    if "distribution" in str(signal.get("source", "")).lower():
        return "SHORT"
    
    # Regime + price action
    price_change_24h = signal.get("price_change_24h_pct", 0)
    
    # BEAR regime + down-trending majors → SHORT opportunity
    if regime_tag in ("BEAR", "BEAR_HIGH_VOL"):
        if signal.get("token") in ("BTC", "ETH", "SOL", "BNB") and price_change_24h < -3:
            return "SHORT"
        
        # Meme coins pumping in bear → fade (SHORT)
        if price_change_24h > 50 and signal.get("volume_24h_usd", 0) < 1_000_000:
            return "SHORT"
    
    # Default: LONG (most signals are bullish by nature)
    return "LONG"
```

### Patch 5B: Apply in signal router
```python
# scripts/signal_router.py line 300 (before strategy matching)
    for signal in signals:
+       if not signal.get("direction"):
+           signal["direction"] = infer_direction(signal, regime_tag)
        
        matches = match_signal_to_strategies(signal)
        ...
```

### Expected Impact
- **Before:** 0 SHORT attempts
- **After:** 5-15% of signals classified SHORT (in BEAR regime)
- **Learning:** SHORT strategies get Thompson data

---

## ISSUE 6: Whale Signals Starved

### Evidence
```
Whale signals generated: 54 in last 6h
Router scanned: 30 per run
Router selected: 2 per run (top-2 by score)
```

**Whale-related decisions:** 68 total (mostly Birdeye trending that matched whale-following strategy, NOT direct whale tracker signals)

### Root Cause
Whale tracker signals score lower than CoinGecko/Birdeye trending → never in top-2.

### Patch 6: Diversity constraint in PAPER+LEARN
```python
# scripts/signal_router.py line 450 (after top-N selection)
    # In PAPER+LEARN: enforce signal source diversity
    paper_mode = portfolio.get("mode") == "paper"
    
    if paper_mode and len(selected_signals) >= 2:
        sources = [s.get("source", "") for s in selected_signals]
        
        # If no onchain/whale signal in batch, force-include one
        has_whale = any("whale" in src.lower() or "onchain" in src.lower() for src in sources)
        
        if not has_whale:
            whale_candidates = [s for s in all_filtered_signals 
                               if "whale" in s.get("source", "").lower() or "onchain" in s.get("source", "").lower()]
            
            if whale_candidates:
                # Replace lowest-scoring signal with top whale signal
                selected_signals[-1] = whale_candidates[0]
                log(f"[ROUTER] Diversity: replaced {selected_signals[-1].get('token')} with whale signal {whale_candidates[0].get('token')}")
```

### Expected Impact
- **Before:** ~0 whale signals processed
- **After:** ~1 whale signal per 2-3 batches (33% coverage)
- **Learning:** whale-following gets more Thompson trials

---

## ISSUE 7: Duplicate Rejection Loop

### Evidence
```
HOUSE: 78x rejected
LOBSTAR: 16x (5x in last 3 hours)
TOTO: 20x
TRUMP: 17x
```

### Root Cause
No cooldown → same token rejected repeatedly when it stays trending.

### Patch 7: Rejection cooldown state
```python
# scripts/signal_router.py line 200 (after loading signals)
    # Load rejection cooldown state
    cooldown_path = STATE_DIR / "rejection_cooldown.json"
    cooldown = {}
    if cooldown_path.exists():
        cooldown = json.load(open(cooldown_path))
    
    # Filter out recently rejected tokens
    cooldown_hours = 6
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)).isoformat()
    
    filtered_signals = []
    for sig in signals:
        token = sig.get("token")
        source_key = f"{token}:{sig.get('source', 'unknown')}"
        
        last_reject = cooldown.get(source_key)
        if last_reject and last_reject > cutoff:
            log(f"[ROUTER] Cooldown: skipping {token} (rejected {last_reject[:16]})")
            continue
        
        filtered_signals.append(sig)
    
    log(f"[ROUTER] Cooldown filter: {len(signals)} → {len(filtered_signals)} signals")
    signals = filtered_signals
```

Add to rejection recording:
```python
# scripts/signal_router.py line 700 (after pipeline REJECT)
    if result == "REJECT":
        source_key = f"{signal['token']}:{signal.get('source', 'unknown')}"
        cooldown[source_key] = datetime.now(timezone.utc).isoformat()
        json.dump(cooldown, open(cooldown_path, "w"), indent=2)
```

### Expected Impact
- **Before:** 5 LOBSTAR rejections in 3h (18% of decisions)
- **After:** 1 LOBSTAR rejection every 6h (deduped)
- **Capacity:** Frees 10-15% of pipeline for new tokens

---

## ISSUE 8: Counterfactual Checker Gaps

### Evidence
```
200 rejection entries
7 have price_24h_later (3.5%)
Last 15 entries: all "NOT CHECKED"
```

### Root Cause
Checker cron runs but doesn't fetch follow-up prices for recent rejections.

### Patch 8: Batch update oldest unchecked
```python
# scripts/counterfactual_checker.py (main loop)
    rejections = cf.get("rejections", [])
    
    # Find oldest unchecked entries (rejected >24h ago, not checked)
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    
    unchecked = [
        r for r in rejections
        if r.get("rejected_at", "") < cutoff_24h and not r.get("checked")
    ]
    
    # Process up to 50 per run
    batch_size = 50
    for r in unchecked[:batch_size]:
        token = r.get("token")
        price_at = r.get("price_at_rejection")
        
        try:
            # Fetch current price (Birdeye or Binance)
            current_price = fetch_price(token)
            
            if current_price and price_at:
                pnl = (current_price - price_at) / price_at * 100
                r["price_24h_later"] = current_price
                r["counterfactual_pnl_pct"] = pnl
                r["checked"] = True
                r["checked_at"] = datetime.now(timezone.utc).isoformat()
                
                log(f"[CF] {token}: ${price_at} → ${current_price} ({pnl:+.1f}%)")
        except Exception as e:
            log(f"[CF] Price fetch failed for {token}: {e}")
    
    # Save updated state
    json.dump(cf, open(cf_path, "w"), indent=2)
    
    completion = sum(1 for r in rejections if r.get("checked")) / len(rejections) * 100
    log(f"[CF] Completion: {completion:.1f}% ({sum(1 for r in rejections if r.get('checked'))}/{len(rejections)})")
```

### Expected Impact
- **Before:** 3.5% coverage
- **After:** >70% coverage after 1-2 days
- **Learning:** Can identify missed opportunities vs correct rejections

---

## VALIDATION COMMANDS

### After Patch 1 (max_hold):
```bash
# Create new position, verify max_hold populated
python3 scripts/signal_router.py  # (wait for next execute)
tail -1 state/positions.json | python3 -c "import sys,json; p=json.load(sys.stdin); print('max_hold:', p.get('max_hold_hours'))"
# Expected: max_hold: 6 or 8 (not MISSING)
```

### After Patch 3 (DEX pricing):
```bash
# Verify BP price updates
python3 scripts/position_monitor.py
grep "BP" state/positions.json | python3 -c "import sys,json; p=json.load(sys.stdin); print('current_price:', p.get('current_price'))"
# Expected: current_price changes from entry
```

### After Patch 4 (regime):
```bash
# Verify avoid_strategies applied
tail -50 logs/signal_router.log | grep "avoiding"
# Expected: "Regime BEAR_HIGH_VOL: avoiding ['meme-momentum', 'early-launch']"
```

### After Patch 5 (SHORT):
```bash
# Verify SHORT signals generated
tail -20 execution-logs/decisions.jsonl | grep -c '"direction":"SHORT"'
# Expected: >0
```

### After Patch 7 (dedup):
```bash
# Verify cooldown working
ls -lh state/rejection_cooldown.json
tail -20 logs/signal_router.log | grep "Cooldown"
# Expected: logs showing "skipping X (rejected ...)"
```

---

## EXPECTED THROUGHPUT AFTER FIXES

### Current (Broken):
- Executes/day: 5
- Closes/day: 2-3
- Hold time: 24-49h
- **Days to 50 trades: 50-70 days**

### After All Fixes:
- Executes/day: 8-12 (better TP hits + diversity)
- Closes/day: 6-10 (max_hold enforced + DEX pricing)
- Hold time: 6-12h (strategy-specific)
- **Days to 50 trades: 5-8 days** ✅

### Critical Path:
1. **Patch 1 (max_hold)** → 3× faster closes
2. **Patch 3 (DEX pricing)** → BP and future DEX positions can exit
3. **Patch 2 (TP/SL)** → Tighter targets → faster wins
4. **Patch 7 (dedup)** → 15% more pipeline capacity

**Other patches (4, 5, 6, 8) improve learning quality but don't directly affect turnover.**

---

## IMPLEMENTATION PRIORITY

### 🔴 P0 (Deploy Today):
1. Patch 1A (max_hold in position writer)
2. Patch 3A (DEX price in monitor)
3. Patch 7 (rejection cooldown)

### 🟡 P1 (Deploy Tomorrow):
4. Patch 2 (TP/SL fallback)
5. Patch 4A (regime avoid filter)
6. Patch 6 (whale diversity)

### 🟢 P2 (Next Week):
7. Patch 5 (SHORT inference)
8. Patch 8 (counterfactual batching)

---

## SIGN-OFF CHECKLIST

- [x] Root causes identified with code line numbers
- [x] Evidence from live system state
- [x] Minimal patches (no rewrites)
- [x] Validation commands provided
- [x] Expected impact quantified
- [x] Priority ranked

**Status:** Ready for implementation approval.
