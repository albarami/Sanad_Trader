# Strategy: Whale Following
# Status: PAPER
# Version: 1.0
# Created: 2026-02-16

## Identity
- Name: whale-following
- Type: Smart money tracking / Accumulation following
- Asset Class: Meme coins and mid-cap altcoins
- Chains: Solana, Ethereum, Base

## Thesis
Whale wallets with historically profitable meme coin trades accumulate before major price moves. When 3+ known profitable wallets begin accumulating the same token within a 6-hour window, this signals informed buying that precedes retail discovery. The edge is the information asymmetry — smart money moves before social media hype.

## Entry Conditions (ALL must be true)
1. Sanad trust score >= 70
2. Whale accumulation: 3+ wallets from the tracked profitable whale list buying the same token within 6 hours
3. Whale wallet quality: tracked wallets must have historical win rate > 55% across 20+ meme trades
4. Accumulation pattern: wallets are buying in multiple tranches (not single large buys — suggests conviction, not market impact)
5. No distribution: none of the accumulating whales are simultaneously selling other meme positions (rotation signal vs general exit)
6. Token fundamentals: Sanad rugpull checks PASSED, contract verified, liquidity locked
7. Minimum liquidity: sufficient depth to enter AND exit full position with < 2% slippage
8. Token not in cooldown: no recent (7-day) trade on same token in our history
9. Market regime: not in BEAR_HIGH_VOL (whale following works best in bull/sideways — in bear, even whales get caught)

## Exit Conditions (ANY triggers exit)
1. Take-profit: 2x entry price (100% gain) — conservative target for whale-following
2. Stop-loss: 15% below entry
3. Trailing stop: once position reaches > 15% profit, activate 8% trailing from high-water mark
4. Time-based exit: maximum hold duration 72 hours (whale accumulation plays take longer to develop)
5. Whale distribution: any 2+ of the tracked whales that triggered the entry begin selling — exit immediately
6. Volume death: 24h volume drops below 40% of entry-day volume
7. Sentiment reversal: social sentiment flips negative while whales are no longer accumulating

## Position Sizing
- Cold start (< 30 trades): kelly_default_pct from thresholds.yaml (0.02 = 2% of portfolio)
- After 30 trades: Fractional Kelly (kelly_fraction: 0.50)
- Hard ceiling: max_position_pct from thresholds.yaml (0.10 = 10% of portfolio)
- Whale conviction bonus: if 5+ whales accumulating (vs minimum 3), allow up to 1.25x normal position
- Meme allocation cap: shares the 30% meme allocation cap with all other meme strategies

## Risk Parameters
- Maximum concurrent positions using this strategy: 3
- Daily loss limit: 5% of portfolio (shared across all strategies)
- Maximum drawdown: 15% (from thresholds.yaml)
- Minimum risk-to-reward ratio: 1:2
- Signal age hard stop: 60 minutes (whale accumulation happens over hours, but data must be recent)

## Signal Sources (ranked by expected reliability)
- On-chain whale wallet monitoring (Helius, Solscan): Grade A
- Exchange flow data (Glassnode/CryptoQuant — large deposits/withdrawals): Grade A
- DEX large-trade alerts (trades > $50K on Raydium/Uniswap): Grade B
- Social media whale tracking accounts: Grade C (must be corroborated by on-chain)

## Trade Confidence Score Components
- Whale Quality Score (0-25): average historical win rate of accumulating whales, number of whales, total USD accumulated
- Liquidity Score (0-20): pool depth / order book thickness vs position size
- Accumulation Pattern (0-20): tranching behavior, time distribution, no wash trading
- Token Safety (0-15): rugpull checks, contract quality, deployer history
- Regime Alignment (0-10): bull or sideways preferred
- Historical Pattern Match (0-10): similarity to past winning whale-following setups
- Minimum combined score to proceed: 65

## Whale Tracking Criteria
Wallets qualify for the "profitable whale" tracking list if they meet ALL of:
- Minimum 20 completed meme coin trades in past 90 days
- Win rate > 55% on meme trades
- Average holding period 1-72 hours (active traders, not long-term holders)
- Portfolio value > $500K in tracked tokens
- NOT flagged as known market maker or bot (verified by wallet clustering)

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
- Whale list curation: monthly review of tracked wallets, remove underperformers

## Notes
- This strategy requires whale_tracker.py (Sprint 7) for full implementation
- Until whale tracker is built, whale signals come from manual watchlist + Helius wallet monitoring
- Time-in-Force: GTC (Good-Till-Cancelled) — patience is the edge, not speed
- Works best in bull/sideways regimes where whale accumulation precedes retail FOMO
- In bear markets, even whale accumulation can fail — regime filter is critical
