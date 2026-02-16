# Data Dictionary — Sprint 10.4.9 + 10.5

All JSON schemas used across Sanad Trader v3.0.

## Signal Object
```json
{
  "token": "string — Token symbol (BONK, SOL, PEPE)",
  "source": "string — Signal source (coingecko, dexscreener, telegram_sniffer, meme_radar, fear_greed)",
  "direction": "string — LONG or SHORT",
  "chain": "string — solana, ethereum, binance",
  "token_address": "string — Contract/mint address",
  "thesis": "string — Why this trade (max 500 chars)",
  "timestamp": "string — ISO 8601 UTC",
  "score": "number — Source confidence 0-100",
  "volume_24h": "number — 24h volume USD",
  "market_cap": "number — Market cap USD",
  "price_change_24h": "number — % change",
  "symbol": "string — Exchange symbol (BONKUSDT)"
}
```

## Position Object
```json
{
  "symbol": "string — Trading pair",
  "token": "string — Token name",
  "status": "string — open|closed",
  "direction": "string — LONG|SHORT",
  "entry_price": "number",
  "current_price": "number",
  "quantity": "number",
  "entry_time": "string — ISO 8601",
  "strategy": "string — Strategy name",
  "sanad_score": "number — Trust score at entry",
  "stop_loss": "number",
  "take_profit": "number",
  "pnl_pct": "number — Current P&L %",
  "pnl_usd": "number — Current P&L USD"
}
```

## Trade History Object
```json
{
  "symbol": "string",
  "direction": "string — LONG|SHORT",
  "entry_price": "number",
  "exit_price": "number",
  "quantity": "number",
  "entry_time": "string — ISO 8601",
  "exit_time": "string — ISO 8601",
  "pnl_pct": "number",
  "pnl_usd": "number",
  "strategy": "string",
  "exit_reason": "string — stop_loss|take_profit|trailing_stop|manual|signal",
  "sanad_score": "number"
}
```

## Sanad Verification Result
```json
{
  "trust_score": "number — 0-100",
  "grade": "string — Tawatur|Mashhur|Ahad",
  "source_grade": "string — A|B|C|D|F",
  "chain_integrity": "string — CONNECTED|BROKEN|PARTIAL",
  "corroboration_level": "string — TAWATUR|MASHHUR|AHAD_SAHIH|AHAD_DAIF",
  "sybil_risk": "string — LOW|MEDIUM|HIGH",
  "rugpull_flags": "array of strings",
  "recommendation": "string — PROCEED|CAUTION|BLOCK",
  "source_count": "number",
  "reasoning": "string"
}
```

## Burner Wallet Object
```json
{
  "wallet_id": "string — burner_{trade_id}_{timestamp}",
  "trade_id": "string",
  "public_key": "string — Solana public key",
  "status": "string — CREATED|FUNDED|ACTIVE|SWEPT|ABANDONED",
  "funded_amount_sol": "number",
  "swept_amount_sol": "number",
  "tx_history": "array of {type, tx, timestamp}"
}
```

## Rugpull Scanner Result
```json
{
  "mint": "string — Token mint address",
  "verdict": "string — SAFE|CAUTION|DANGER|RUG|BLACKLISTED",
  "risk_score": "number — 0-100",
  "flags": "array of strings — Pattern names triggered",
  "auto_blacklisted": "boolean"
}
```

## Honeypot Detection Result
```json
{
  "mint": "string",
  "verdict": "string — SAFE|CAUTION|DANGER|HONEYPOT|UNKNOWN",
  "buy_possible": "boolean",
  "sell_possible": "boolean",
  "round_trip_loss_pct": "number",
  "is_honeypot": "boolean"
}
```

## Red Team Attack Result
```json
{
  "attack": "string — Category/name",
  "passed": "boolean — True = system defended",
  "verdict": "string — DEFENDED|VULNERABLE",
  "details": "object — Attack-specific data",
  "timestamp": "string — ISO 8601"
}
```

## Alert Levels

| Level | Name | Destinations | Trigger |
|-------|------|--------------|---------|
| L1 | INFO | Console only | Heartbeat, routine events |
| L2 | NORMAL | Telegram | Trade alerts, daily reports |
| L3 | URGENT | Telegram + emphasis | Rejections, warnings |
| L4 | EMERGENCY | Telegram + action | Flash crash, kill switch |

## State Files

| File | Purpose | Updated By |
|------|---------|------------|
| portfolio.json | Balance, mode, equity | heartbeat, trades |
| positions.json | Open positions | trade execution |
| trade_history.json | Closed trades | trade close |
| heartbeat_state.json | Last heartbeat | heartbeat.py |
| policy_engine_state.json | Gates, kill switch | policy_engine.py |
| price_cache.json | Latest prices | price_snapshot.py |
| rugpull_db.json | Known rug contracts | rugpull_db.py |
| social_sentiment_state.json | Sentiment scores | social_sentiment.py |
| daily_root_hash.json | Integrity hash | red_team.py |
| pending_commands.json | Console commands | console_api.py |
