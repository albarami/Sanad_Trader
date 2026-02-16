# Risk Management: Master Risk File
# Status: ACTIVE
# Version: 1.0
# Created: 2026-02-16

## Identity
- Name: risk-management
- Type: Master risk parameters (overrides all strategy-level risk settings)
- Scope: Portfolio-wide, all strategies, all tokens, all chains

## Core Principle
This file defines the ABSOLUTE risk boundaries for Sanad Trader. These limits CANNOT be exceeded by any strategy, agent, or learning system. The Genius Memory Engine can TIGHTEN these parameters but NEVER loosen them. Any violation of these limits triggers an immediate system halt.

## Portfolio-Level Limits

### Position Limits
- Maximum single position: 10% of portfolio (max_position_pct: 0.10)
- Maximum meme coin exposure: 30% of portfolio (max_meme_allocation_pct: 0.30)
- Maximum total exposure: 60% of portfolio (remaining 40% always in stablecoins/cash)
- Maximum concurrent open positions: 5 across all strategies
- Maximum same-token exposure: 10% (no doubling down via different strategies)

### Loss Limits
- Daily loss limit: 5% of portfolio (daily_loss_limit_pct: 0.05) — all trading halts until next UTC day
- Weekly loss limit: 10% of portfolio — all trading halts until next Monday UTC
- Maximum drawdown: 15% (max_drawdown_pct: 0.15) — system enters PAUSED mode, requires manual review
- Maximum consecutive losses before pause: 5 trades — system pauses for 4 hours of cooling period

### Per-Trade Limits
- Default stop-loss: 15% below entry (stop_loss_default_pct: 0.15)
- Maximum stop-loss: 25% (only early-launch strategy may use this wider stop)
- Minimum risk-to-reward ratio: 1:2 (no trade where potential loss > 50% of potential gain)
- Maximum slippage tolerance: CEX 1.5%, DEX 3%

## Position Sizing Rules

### Kelly Criterion
- Cold start (< 30 trades): fixed 2% of portfolio (kelly_default_pct: 0.02)
- After 30 trades: fractional Kelly at 0.50 (half-Kelly, the recommended conservative approach)
- Kelly fraction: 0.50 (kelly_fraction in thresholds.yaml) — full Kelly is too aggressive
- Kelly cap: position size from Kelly calculation is CAPPED at max_position_pct regardless of edge estimate

### Regime-Based Modifiers
- BULL_LOW_VOL: 1.1x normal sizing (favorable conditions)
- BULL_HIGH_VOL: 0.8x (bullish but volatile — reduce)
- BULL_NORMAL_VOL: 1.0x (baseline)
- SIDEWAYS any vol: 0.9x (reduced conviction)
- BEAR_LOW_VOL: 0.5x (defensive)
- BEAR_NORMAL_VOL: 0.4x (very defensive)
- BEAR_HIGH_VOL: 0.3x (capital preservation — current regime)

### Strategy-Specific Modifiers
- meme-momentum: 1.0x (baseline strategy)
- early-launch: 0.5x (highest risk, smallest positions)
- whale-following: 1.0x (baseline)
- sentiment-divergence: 1.0x (baseline)
- cex-listing-play: 1.0x (baseline, with tier bonuses per strategy file)

## Circuit Breakers

### System-Level
- Daily loss limit hit → ALL trading halted until next UTC day
- Weekly loss limit hit → ALL trading halted until next Monday 00:00 UTC
- Max drawdown hit → System enters PAUSED mode, requires manual SSH approval to resume
- Policy engine failure → ALL trading halted, heartbeat alert fired
- Price feed stale (> 10 min) → ALL exit monitoring continues but NO new entries

### Per-Strategy
- 5 consecutive losses on one strategy → strategy paused for 24 hours
- Win rate below 30% over last 20 trades → strategy demoted to PAPER mode
- Strategy auto-retires after 30 consecutive days below 40% win rate

### Per-Token
- 2 losses on same token within 7 days → token enters 14-day cooldown
- Token trades within 24 hours of previous exit → BLOCKED (prevents revenge trading)

## Correlation & Concentration

### Sector Limits
- Maximum exposure to tokens sharing the same narrative/sector: 20% of portfolio
- Example: if PEPE and DOGE are both "dog coins," combined exposure cannot exceed 20%

### Chain Limits
- Maximum Solana DEX exposure: 20% of portfolio (higher execution risk)
- Maximum EVM DEX exposure: 20% of portfolio
- Maximum CEX exposure: 40% of portfolio

### Exchange Limits
- Maximum exposure to any single exchange: 40% of portfolio
- If Binance is down, MEXC positions can continue but no new Binance entries

## Emergency Procedures

### Kill Switch Conditions (immediate halt ALL activity)
- Heartbeat failure (3 consecutive missed heartbeats)
- Reconciliation mismatch detected (state vs exchange discrepancy)
- Balance discrepancy > 1% between tracked and actual
- Exchange API returning errors for > 5 minutes continuously
- Any position showing > 30% unrealized loss (circuit breaker)

### Recovery Protocol
1. Kill switch triggered → ALL cron jobs pause new entries (exits continue)
2. Reconciliation runs immediately to identify discrepancies
3. WhatsApp alert sent to operator
4. Manual SSH review required before resuming
5. Resume requires explicit `RESUME` command — never auto-resumes

## Learning System Guardrails

### What Genius Memory CAN Do
- Tighten stop-losses (make them tighter, not wider)
- Reduce position sizes
- Add tokens to cooldown list
- Recommend strategy parameter changes (with evidence)
- Identify underperforming sources and lower their UCB1 scores

### What Genius Memory CANNOT Do
- Increase max_position_pct beyond 10%
- Reduce stop-loss below the minimum (widen stops beyond 25%)
- Increase max_meme_allocation_pct beyond 30%
- Raise daily_loss_limit_pct beyond 5%
- Override kill switch conditions
- Remove circuit breakers
- Any change requires minimum 30 trades of supporting evidence

## Audit Requirements

### Every Trade Must Log
- Full DecisionPacket with all agent outputs
- Entry rationale with confidence score
- Exit rationale with exit quality assessment
- P&L calculation (Python, not LLM-estimated)
- Regime tag at entry and exit
- Source attribution for UCB1 update
- Hash-chained event to Supabase

### Weekly Review (Sunday Automated)
- Sharpe ratio calculation (Python sandbox)
- Maximum drawdown recalculation
- Strategy win rate by regime
- Source accuracy review (UCB1 recalculation)
- Regime classification accuracy
- Any parameter change proposals (with evidence and counterfactual)

## Reference to thresholds.yaml
All numerical values in this file correspond to entries in config/thresholds.yaml. If there is ANY conflict between this file and thresholds.yaml, the MORE CONSERVATIVE value wins. The policy engine enforces thresholds.yaml programmatically; this file provides the reasoning and context for those numbers.

## Notes
- This file is the risk constitution. Strategies propose, risk management disposes.
- "Better to miss 10 profitable trades than take 1 trade that blows up the portfolio."
- All risk parameters were set BEFORE any trading begins — they are not backtested-optimized
- Risk parameters reflect the reality that meme coins can lose 90%+ in hours
- The system is designed to survive a 50% portfolio drawdown and recover — but the goal is to NEVER reach that point
