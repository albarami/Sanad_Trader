# Strategy: CEX Listing Play
# Status: PAPER
# Version: 1.0
# Created: 2026-02-16

## Identity
- Name: cex-listing-play
- Type: Event-driven / Catalyst
- Asset Class: Meme coins and altcoins with upcoming CEX listings
- Chains: Solana, Ethereum, Base, BSC (pre-listing on DEX), then CEX post-listing

## Thesis
When a meme coin announces or is rumored for a major CEX listing (Binance, Coinbase, MEXC, Bybit), it typically pumps 30-100%+ in the hours before listing and 10-50% in the first minutes after listing goes live. Historical pattern: the pump peaks within 30-60 minutes of listing, followed by a sell-the-news dump. The edge is entering before the listing and exiting within the first hour — capturing the anticipation pump and early listing momentum while avoiding the inevitable dump.

## Entry Conditions (ALL must be true)
1. Sanad trust score >= 70
2. Listing signal: confirmed announcement from the exchange's official channels (not rumor alone — unless corroborated by 2+ independent sources)
3. Listing timing: entry must be at least 1 hour before listing time (too close = price already pumped)
4. Token tradeable: token must currently be tradeable on at least one DEX or smaller CEX
5. Rugpull safety: ALL checks PASSED
6. Pre-listing price: token has NOT already pumped > 100% since listing announcement (late entry = exit liquidity)
7. Minimum liquidity: current trading venue has >= $100K liquidity depth
8. Exchange credibility: listing exchange is Tier 1 (Binance, Coinbase) or Tier 2 (MEXC, Bybit, OKX) — no micro-exchanges
9. No recent listing failures: token has not been listed and then delisted from another CEX in past 30 days

## Exit Conditions (ANY triggers exit)
1. Take-profit: 2x entry price (100% gain) — primary target
2. Time-based exit: maximum 1 hour after listing goes live — HARD EXIT regardless of P&L
3. Pre-listing stop: if listing is delayed or cancelled, exit immediately at market
4. Stop-loss: 15% below entry
5. Trailing stop: once position reaches > 20% profit, activate 8% trailing from high-water mark
6. Volume spike reversal: if a massive sell candle appears (> 10% drop in 5 minutes) in the first hour post-listing, exit immediately
7. Spread blowout: if bid-ask spread on the new CEX listing exceeds 3%, exit (indicates thin liquidity or manipulation)

## Position Sizing
- Cold start (< 30 trades): kelly_default_pct from thresholds.yaml (0.02 = 2% of portfolio)
- After 30 trades: Fractional Kelly (kelly_fraction: 0.50)
- Hard ceiling: max_position_pct from thresholds.yaml (0.10 = 10% of portfolio)
- Tier bonus: Binance/Coinbase listings get 1.2x normal position (higher reliability), MEXC/Bybit get 1.0x
- Meme allocation cap: shares the 30% meme allocation cap with all other meme strategies

## Risk Parameters
- Maximum concurrent positions using this strategy: 2 (listings are infrequent, concentrate when they occur)
- Daily loss limit: 5% of portfolio (shared across all strategies)
- Maximum drawdown: 15% (from thresholds.yaml)
- Minimum risk-to-reward ratio: 1:2
- Signal age hard stop: 120 minutes (listing rumors have longer shelf life, but must be corroborated)

## Signal Sources (ranked by expected reliability)
- Official exchange announcements (Binance, Coinbase blogs): Grade A
- Exchange API listing endpoints (if available): Grade A
- Twitter/X from exchange official accounts: Grade A
- Telegram alpha groups with listing intel: Grade B
- Twitter/X crypto news aggregators: Grade C
- Reddit listing rumors: Grade D (require corroboration)

## Trade Confidence Score Components
- Listing Credibility (0-30): official announcement > multiple rumor sources > single rumor
- Exchange Tier (0-20): Binance/Coinbase = 20, MEXC/Bybit = 15, smaller = 10
- Price Position (0-15): how much has the token already pumped since announcement (less = better)
- Liquidity Score (0-15): current depth on available trading venues
- Token Quality (0-10): Sanad score, rugpull checks, community size
- Time to Listing (0-10): optimal window 2-12 hours before listing
- Minimum combined score to proceed: 65

## Execution Notes
- Pre-listing entry: buy on DEX (if Solana/EVM) or smaller CEX where token is already listed
- Post-listing: monitor the new CEX listing for exit — volume and price discovery on the major CEX
- Order type: IOC (Immediate-Or-Cancel) for time-sensitive execution
- If entering on DEX: Jito MEV bundle required (prevent frontrunning during high-activity period)
- If entering on smaller CEX: limit order at best ask + 0.1% for guaranteed fill

## Thompson Sampling Parameters
- Initial alpha: 1
- Initial beta: 1
- Selection method during PAPER mode: thompson
- Selection method after 30 days LIVE: exploitation

## Strategy Evolution Rules
- Minimum 30 trades before any parameter change (may take months — listings are infrequent)
- Maximum 1 parameter change per week
- Auto-revert if win rate degrades > 10% over evaluation window
- All changes logged to genius-memory/strategy-evolution/ with evidence

## Notes
- This is an event-driven strategy — signals are infrequent but high-conviction
- The 1-hour post-listing hard exit is NON-NEGOTIABLE — overstaying is the primary loss driver
- Listing delays/cancellations are the main risk — the pre-listing stop protects against this
- Time-in-Force: GTC pre-listing (need order to sit), IOC post-listing (speed matters)
- Track listing-to-price-action patterns in genius-memory for continuous edge refinement
- CEX listing announcements sometimes leak early on Telegram — this is a key signal source
