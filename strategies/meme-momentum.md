# Strategy: Meme Coin Momentum
# Status: ACTIVE
# Version: 1.0
# Created: 2025-02-15

## Identity

- Name: meme-momentum
- Type: Momentum / Trend-following
- Asset Class: Meme coins (CEX-listed on Binance/MEXC)
- Chains: EVM (Ethereum, Base, BSC), Solana

## Entry Conditions (ALL must be true)

1. Sanad trust score >= 70
2. Verified social momentum: rising mention count across 2+ platforms (Twitter, Telegram, Reddit) within last 4 hours
3. Rising DEX or CEX volume: current 4-hour volume > 2x the previous 4-hour volume
4. Whale accumulation detected: 2+ wallets with >$50K balance buying within last 6 hours (verified via on-chain data, not social claims)
5. Rugpull safety check: PASSED (all checks from sanad-verifier.md)
6. Liquidity check: sufficient depth to exit full position with < 3% slippage
7. Token age: > 24 hours since first trade (avoid extreme early-launch volatility)

## Exit Conditions (ANY triggers exit)

1. Take-profit: 2x entry price (100% gain)
2. Stop-loss: 15% below entry price (from thresholds.yaml stop_loss_default_pct)
3. Trailing stop: once position reaches > 15% profit, activate 8% trailing stop from high-water mark
4. Time-based exit: maximum hold duration 48 hours — if target not hit, exit at market
5. Volume death signal: current trading volume drops below 30% of entry volume
6. Whale exit detection: whale wallets that were accumulating begin distributing (selling)
7. Sentiment reversal: social sentiment flips from positive to negative while position is open

## Position Sizing

- Cold start (< 30 trades): use kelly_default_pct from thresholds.yaml (0.02 = 2% of portfolio)
- After 30 trades: Fractional Kelly (kelly_fraction: 0.50 from thresholds.yaml)
- Hard ceiling: max_position_pct from thresholds.yaml (0.10 = 10% of portfolio)
- Meme allocation cap: max_meme_allocation_pct from thresholds.yaml (0.30 = 30% total meme exposure)

## Risk Parameters

- Maximum concurrent positions using this strategy: 3
- Daily loss limit: 5% of portfolio (from thresholds.yaml daily_loss_limit_pct)
- Maximum drawdown: 15% (from thresholds.yaml max_drawdown_pct)
- Minimum risk-to-reward ratio: 1:2

## Signal Sources (ranked by expected reliability)

- On-chain data (whale wallets, exchange flows): Grade A
- DEX/CEX volume and price data: Grade A
- Twitter/X verified accounts with >100K followers: Grade B
- Telegram alpha groups (curated list): Grade C
- Reddit trending: Grade C
- Influencer calls: Grade D (require Tawatur corroboration)

## Trade Confidence Score Components

- Liquidity Score (0-20): pool depth / order book thickness vs position size
- Volatility Compatibility (0-20): momentum strategies score higher in rising volatility
- Expected Value (0-30): risk/reward ratio from entry, stop-loss, take-profit
- Regime Alignment (0-15): bull or early-bull regime preferred
- Historical Pattern Match (0-15): similarity to past winning momentum setups
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

## Notes

- This is the primary paper trading strategy for CEX meme coin momentum plays
- Designed to capture mid-cycle momentum, NOT early launches (see early-launch.md for that)
- All numerical thresholds reference thresholds.yaml as single source of truth
- Strategy parameters may be tightened (never loosened) by the Genius Memory Engine
