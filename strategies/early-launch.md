# Strategy: Early Launch
# Status: PAPER
# Version: 1.0
# Created: 2026-02-16

## Identity
- Name: early-launch
- Type: Early-stage momentum / New token launch
- Asset Class: Newly launched meme coins (Pump.fun, Raydium new pools)
- Chains: Solana (primary), Base (secondary)

## Thesis
New token launches on Pump.fun and Raydium present asymmetric risk/reward. Tokens that pass all rugpull checks, show organic buying patterns (not bot-driven), and have bonding curve progression above 50% have historically delivered 3-5x returns within hours. The edge decays rapidly — signal age must be under 10 minutes.

## Entry Conditions (ALL must be true)
1. Sanad trust score >= 65 (lower than momentum due to limited data, compensated by smaller position)
2. Token age: between 10 minutes and 4 hours since first trade (avoid snipers and stale launches)
3. Bonding curve progression: above 50% (demonstrates genuine demand, not just deployer liquidity)
4. Organic buying pattern: ratio of unique buyers to total transactions > 0.4 (filters bot-wash-trading)
5. Rugpull safety: ALL checks PASSED — contract verified, no mint authority, no freeze authority, liquidity locked or burned
6. Sybil check: first 50 buyers are NOT clustered from same funding source (Helius wallet analysis)
7. Deployer history: deployer wallet has NOT deployed >3 tokens in past 7 days (rug factory filter)
8. Minimum liquidity: pool has >= $25,000 in paired token (SOL/USDC)
9. No honeypot: simulated BUY + SELL via Helius both succeed with < 5% tax
10. Fear & Greed >= 30 (avoid launching into extreme fear — new tokens die fastest in bear)

## Exit Conditions (ANY triggers exit)
1. Take-profit: 3x entry price (200% gain) — primary target
2. Stretch target: scale out 50% at 3x, remaining 50% rides to 5x with trailing stop
3. Stop-loss: 25% below entry (wider than momentum due to early-stage volatility)
4. Trailing stop: once position reaches > 30% profit, activate 15% trailing from high-water mark
5. Time-based exit: maximum hold duration 4 hours — hard exit regardless of P&L
6. Bonding curve reversal: if bonding curve drops below 30% after entry, exit immediately
7. Liquidity drain: pool liquidity drops below 50% of entry-time liquidity
8. Volume death: trading volume drops below 20% of first-hour volume
9. Flash crash: price drops > 15% in 5 minutes — emergency exit

## Position Sizing
- Cold start (< 30 trades): kelly_default_pct × 0.5 = 1% of portfolio (half the normal allocation)
- After 30 trades: Fractional Kelly (kelly_fraction: 0.50) with 0.5x modifier
- Hard ceiling: max_position_pct × 0.5 = 5% of portfolio (half the normal ceiling)
- Total early-launch allocation cap: 10% of portfolio across all concurrent early-launch positions
- Minimum position: $50 (below this, fees eat the edge)

## Risk Parameters
- Maximum concurrent positions using this strategy: 2
- Daily loss limit: 3% of portfolio for early-launch trades specifically
- Maximum drawdown contribution: 5% (strategy gets paused if it drags portfolio drawdown above this)
- Minimum risk-to-reward ratio: 1:3 (wider stops require higher targets)
- Maximum slippage tolerance: 3% (DEX trades have higher slippage)

## Signal Sources (ranked by expected reliability)
- Pump.fun bonding curve data (Helius WebSocket): Grade A
- On-chain transaction analysis (unique buyers, wallet clustering): Grade A
- DEX pool creation events (Raydium, Orca): Grade B
- Telegram alpha groups (curated list): Grade C
- Twitter/X contract address posts: Grade D (require rapid Sanad verification)

## Trade Confidence Score Components
- Liquidity Score (0-20): pool depth vs position size, paired token quality (SOL > random token)
- Safety Score (0-25): rugpull checks passed, deployer clean, no honeypot, no Sybil
- Organic Activity (0-20): unique buyers ratio, holding duration distribution, no wash trading
- Bonding Curve Health (0-15): progression %, velocity, not artificially inflated
- Regime Alignment (0-10): neutral or bull regime preferred, bear regime penalized
- Historical Pattern Match (0-10): similarity to past winning early-launch setups
- Minimum combined score to proceed: 60

## Thompson Sampling Parameters
- Initial alpha: 1
- Initial beta: 1
- Selection method during PAPER mode: thompson
- Selection method after 30 days LIVE: exploitation

## Strategy Evolution Rules
- Minimum 30 trades before any parameter change
- Maximum 1 parameter change per week
- Auto-revert if win rate degrades > 10% over evaluation window
- All changes logged to genius-memory/strategy-evolution/ with evidence
- Parameter tightening only — never loosen stops or increase position sizes

## Notes
- This strategy has the HIGHEST risk and requires the smallest position sizes
- Signal age hard stop: 10 minutes — stale signals are actively dangerous
- DEX execution only — these tokens are not on CEX
- All trades routed through Jito MEV bundles when available (prevent frontrunning)
- Burner wallets required for each trade (prevent wallet profiling)
- Paper mode must demonstrate 30+ trades with positive expectancy before Shadow promotion
