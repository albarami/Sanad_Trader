# Config Specification — thresholds.yaml (10.4.8)

## Risk Management
```yaml
risk:
  max_drawdown_pct: 15       # Kill switch if portfolio drops >15%
  flash_crash_pct: 20        # Emergency if BTC/SOL drops >20% in 1h
  stop_loss_pct: 5           # Per-position stop loss
  take_profit_pct: 15        # Per-position take profit
  trailing_stop_pct: 3       # Trailing stop activation
  max_position_pct: 10       # Max % of portfolio per position
  max_positions: 5           # Max concurrent positions
  max_daily_trades: 10       # Max trades per day
  cooldown_minutes: 30       # Min time between trades on same token
```

## Sizing
```yaml
sizing:
  kelly_fraction: 0.25       # Quarter-Kelly for safety
  min_trade_usd: 10          # Min trade size
  max_trade_usd: 500         # Max trade size
  base_position_pct: 2       # Default position as % of portfolio
```

## Execution
```yaml
execution:
  max_slippage_bps: 300      # Max 3% slippage
  priority_fee_lamports: auto # Helius auto-adjusts
  retry_attempts: 3          # Retry failed orders
  retry_delay_seconds: 2     # Delay between retries
  paper_mode: true           # PAPER/SHADOW/LIVE
```

## Signal Quality
```yaml
signals:
  min_sanad_score: 70        # Hard floor — below = BLOCK
  min_source_grade: C        # Minimum source reliability grade
  max_signal_age_hours: 24   # Reject signals older than this
  min_cross_confirmations: 2 # Need 2+ independent sources
  stale_price_minutes: 5     # Price data older than this = stale
```

## Data Quality
```yaml
data:
  price_outlier_threshold: 3  # 3 sigma = outlier
  cross_feed_tolerance_pct: 2 # Max % diff between price feeds
  min_feeds_required: 2       # Need 2+ price feeds active
  heartbeat_interval_min: 10  # Heartbeat every 10 minutes
  reconciliation_interval_min: 60
```

## API Budget
```yaml
budget:
  daily_limit_usd: 50        # Max API spend per day
  anthropic_max_calls: 100    # Max Claude calls per day
  openai_max_calls: 50        # Max GPT calls per day
  perplexity_max_calls: 50    # Max Perplexity calls per day
```

## Notification Levels
```yaml
notifications:
  L1_INFO: console_only       # Heartbeat, routine
  L2_NORMAL: telegram          # Trade alerts, daily reports
  L3_URGENT: telegram_emphasis # Rejections, warnings
  L4_EMERGENCY: telegram_action # Flash crash, kill switch
```
