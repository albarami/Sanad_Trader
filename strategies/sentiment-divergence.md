# Strategy: Sentiment Divergence
# Status: PAPER
# Version: 1.0
# Created: 2026-02-16

## Identity
- Name: sentiment-divergence
- Type: Contrarian / Mean-reversion
- Asset Class: Meme coins and mid-cap altcoins with social presence
- Chains: Solana, Ethereum, Base, BSC

## Thesis
When on-chain accumulation diverges from negative social sentiment — smart money buying while retail is fearful — this creates asymmetric entry opportunities. The divergence signals that informed participants see value that the crowd has missed or abandoned. Historical pattern: sentiment-driven sell-offs in fundamentally active tokens recover within 24-72 hours when on-chain metrics remain healthy.

## Entry Conditions (ALL must be true)
1. Sanad trust score >= 70
2. Social sentiment: negative or declining across 2+ platforms (Twitter, Telegram, Reddit) — measured by sentiment score < 35/100
3. On-chain divergence: despite negative sentiment, net token flow is positive (more buying than selling by unique wallets in last 12 hours)
4. Whale behavior: whale wallets are NOT distributing — either accumulating or holding steady
5. Volume floor: 24h trading volume remains above $500K (token is still liquid and traded, not dead)
6. Price decline: token has dropped 15-40% from recent 7-day high (enough pain to create opportunity, not so much to signal fundamental failure)
7. Rugpull safety: ALL checks PASSED
8. No material negative catalyst: the sentiment decline is fear-driven, NOT caused by actual exploit, team exit, or contract vulnerability
9. Fear & Greed <= 45 (sentiment divergence works best when overall market sentiment is fearful or neutral)

## Exit Conditions (ANY triggers exit)
1. Take-profit: 1.5x entry price (50% gain) — conservative for contrarian plays
2. Extended target: if momentum develops, switch to trailing stop after 30% gain
3. Stop-loss: 20% below entry (wider stop — contrarian trades need room to develop)
4. Trailing stop: once position reaches > 30% profit, activate 10% trailing from high-water mark
5. Time-based exit: maximum hold duration 72 hours
6. Sentiment confirmation failure: if on-chain buying reverses (net negative flow) within 12 hours of entry, exit regardless of price
7. Whale distribution: tracked whale wallets begin selling the token
8. Volume death: 24h volume drops below $200K (liquidity dying)

## Position Sizing
- Cold start (< 30 trades): kelly_default_pct from thresholds.yaml (0.02 = 2% of portfolio)
- After 30 trades: Fractional Kelly (kelly_fraction: 0.50)
- Hard ceiling: max_position_pct from thresholds.yaml (0.10 = 10% of portfolio)
- Conviction scaling: if divergence score > 80 (strong divergence), allow up to 1.2x normal position
- Meme allocation cap: shares the 30% meme allocation cap with all other meme strategies

## Risk Parameters
- Maximum concurrent positions using this strategy: 2 (contrarian trades require more attention)
- Daily loss limit: 5% of portfolio (shared across all strategies)
- Maximum drawdown: 15% (from thresholds.yaml)
- Minimum risk-to-reward ratio: 1:2
- Signal age hard stop: 60 minutes

## Signal Sources (ranked by expected reliability)
- On-chain net flow analysis (Helius, Dune): Grade A
- DEX volume and unique buyer counts: Grade A
- Social sentiment aggregation (Twitter, Telegram, Reddit): Grade B
- Fear & Greed Index context: Grade B
- Whale wallet holding patterns: Grade B

## Trade Confidence Score Components
- Divergence Strength (0-25): magnitude of gap between sentiment (negative) and on-chain (positive)
- On-Chain Health (0-20): unique buyers, holder count trend, net flow direction
- Liquidity Score (0-15): sufficient depth for entry and exit
- Price Level (0-15): how far from recent high (sweet spot: 15-40% decline)
- Regime Alignment (0-15): works in fear/neutral regimes, penalized in extreme greed
- Historical Pattern Match (0-10): similarity to past winning divergence setups
- Minimum combined score to proceed: 65

## Divergence Score Calculation
The divergence score (0-100) measures the gap between sentiment and on-chain:
- Social sentiment component (0-50): lower sentiment = higher score (inverse)
- On-chain activity component (0-50): more buying activity despite sentiment = higher score
- Components: unique buyer ratio, net flow direction, whale holding stability, volume maintenance
- Minimum divergence score to trigger signal: 60

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
- This is the primary contrarian/mean-reversion strategy
- Works best in BEAR_LOW_VOL and SIDEWAYS regimes where fear creates opportunities
- Requires twitter_tracker.py (Sprint 7) and social sentiment aggregation for full implementation
- Until social feeds are built, divergence signals rely on Fear & Greed + on-chain data only
- Key risk: "catching a falling knife" — the stop-loss and sentiment confirmation failure exit guard against this
- This strategy MUST be disabled during genuine black swan events (exchange hacks, regulatory crackdowns)
