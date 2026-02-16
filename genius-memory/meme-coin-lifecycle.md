# Meme Coin Lifecycle Model — Sprint 5.2.8

## Purpose
Reference document for strategy agents. Describes typical meme coin phases so the system can identify WHERE in the lifecycle a token currently sits.

## The 7 Phases

### Phase 1: Stealth Launch (0-30 min)
- **What happens:** Token deployed, initial liquidity added, insider wallets accumulate
- **Signals:** New pair on DEX, <$10K liquidity, <50 holders
- **Strategy:** Early Launch only. Ultra-small position (0.5% max). High rug risk.
- **Exit trigger:** If no social traction within 30 min, exit immediately

### Phase 2: Discovery (30 min - 4 hours)
- **What happens:** First organic buyers, CT mentions begin, volume spikes
- **Signals:** 50-500 holders, $10K-$100K liquidity, first social mentions
- **Strategy:** Early Launch or Meme Momentum. Position ≤1%.
- **Key risk:** 80% of tokens die here. Rug pulls peak in this phase.

### Phase 3: Viral Spread (4-24 hours)
- **What happens:** Crypto Twitter catches on, influencer mentions, rapid holder growth
- **Signals:** 500-5K holders, $100K-$1M liquidity, trending on DEXScreener
- **Strategy:** Meme Momentum. Best risk/reward phase. Position ≤2%.
- **Exit trigger:** Whale sells >5% of supply, sentiment reversal

### Phase 4: CEX Listing Speculation (1-7 days)
- **What happens:** Community lobbies for CEX listing, price pumps on rumors
- **Signals:** 5K-50K holders, >$1M liquidity, CEX listing announcements
- **Strategy:** CEX Listing Play. Enter pre-listing, exit 1hr post-listing.
- **Key risk:** "Buy the rumor, sell the news" — price often dumps after listing

### Phase 5: Peak Mania (hours to days)
- **What happens:** Peak social mentions, mainstream media attention, FOMO buying
- **Signals:** Extreme greed on social, holder growth slows, whale distribution begins
- **Strategy:** EXIT ONLY. Never enter at peak mania. Tighten all stops.
- **Warning signs:** Influencers shilling aggressively, "this is the next DOGE" narratives

### Phase 6: Distribution & Decline (days to weeks)
- **What happens:** Smart money exits, retail holds bags, volume drops
- **Signals:** Declining volume, whale wallets emptying, negative sentiment shift
- **Strategy:** No entry. If holding, strict trailing stop (8% offset).
- **Key pattern:** Series of lower highs, bounces get weaker

### Phase 7: Death or Zombie (weeks to months)
- **What happens:** Token either dies (99% drop) or finds a floor with loyal community
- **Signals:** <$50K daily volume, flat price, minimal social activity
- **Strategy:** No trade. Occasional zombie pumps (avoid — low probability).
- **Exception:** If project pivots to utility (rare), may re-enter Discovery phase

## Phase Detection Heuristics

| Indicator | Phase 1-2 | Phase 3-4 | Phase 5 | Phase 6-7 |
|-----------|-----------|-----------|---------|-----------|
| Holders | <500 | 500-50K | >50K | Declining |
| Liquidity | <$100K | $100K-$5M | >$5M | Declining |
| Volume trend | Spiking | Sustained | Peaking | Declining |
| Social mentions | Emerging | Viral | Peak | Fading |
| Whale behavior | Accumulating | Holding | Distributing | Exited |
| Risk level | EXTREME | HIGH | VERY HIGH | HIGH |
| Opportunity | Highest (if legit) | Best R:R | Exit only | None |

## Key Rules for Sanad Trader

1. **Never enter Phase 5 or later** — the system must detect mania and refuse entry
2. **Phase 1-2 = maximum rug risk** — Sanad verification critical, tiny positions only
3. **Phase 3 = sweet spot** — best risk/reward, but time window is short
4. **Phase 4 = specific strategy only** — CEX listing play, pre-defined exit
5. **Whale behavior is the leading indicator** — on-chain data beats social sentiment
6. **Volume is truth** — social hype without volume = manipulation
7. **Speed matters** — meme coins move faster than traditional crypto. Signal → Decision → Execution must complete in <5 minutes.

## Integration Points

- **Signal Layer:** Phase detection feeds into signal scoring
- **Strategy Selection:** Thompson sampler uses phase as input
- **Risk Sizing:** Kelly criterion adjusts for phase risk
- **Exit Manager:** Phase transition triggers exit review
- **Genius Memory:** Tag every trade with entry/exit phase for learning

---

*This document is a living reference. Updated as the system learns from real trades.*
