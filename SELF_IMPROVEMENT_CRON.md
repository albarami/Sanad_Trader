# Self-Improvement Components — Recommended Cron Jobs

All 4 self-improvement components have been built and committed. Below are the recommended cron schedules.

## Installation Instructions

Add these lines to your crontab (run `crontab -e`):

```bash
# Set SANAD_HOME for all cron jobs
SANAD_HOME=/data/.openclaw/workspace/trading

# Component 1: Daily Deep Research
# Run once per day at 6 AM UTC
0 6 * * * cd $SANAD_HOME/scripts && python3 daily_deep_research.py >> $SANAD_HOME/logs/daily_research.log 2>&1

# Component 2: Pattern Extractor
# Run every 6 hours (at 00:00, 06:00, 12:00, 18:00 UTC)
0 */6 * * * cd $SANAD_HOME/scripts && python3 pattern_extractor.py >> $SANAD_HOME/logs/pattern_extractor.log 2>&1

# Component 3: Prompt Optimizer
# Run weekly on Monday at 3 AM UTC
0 3 * * 1 cd $SANAD_HOME/scripts && python3 prompt_optimizer.py >> $SANAD_HOME/logs/prompt_optimizer.log 2>&1

# Component 4: Regime Adapter
# Run every hour to check for regime changes
0 * * * * cd $SANAD_HOME/scripts && python3 regime_adapter.py >> $SANAD_HOME/logs/regime_adapter.log 2>&1
```

## Alternative: Every 50 Trades Trigger for Prompt Optimizer

If you want prompt optimization to trigger based on trade count instead of weekly, replace the Component 3 cron with:

```bash
# Component 3: Prompt Optimizer (every 12 hours, checks trade count)
0 */12 * * * cd $SANAD_HOME/scripts && python3 prompt_optimizer.py >> $SANAD_HOME/logs/prompt_optimizer.log 2>&1
```

Then modify `prompt_optimizer.py` to check trade count threshold (50 new trades) similar to how `pattern_extractor.py` checks for 20 new trades.

## Component Details

### 1. Daily Deep Research (`daily_deep_research.py`)
- **Frequency:** Daily at 6 AM UTC
- **Purpose:** Gather alpha discovery, regime intelligence, risk radar reports
- **Outputs:** 
  - `reports/daily-research/YYYY-MM-DD.json` (full report)
  - `genius-memory/research/latest.json` (condensed)
- **Notifications:** Telegram L2 with highlights

### 2. Pattern Extractor (`pattern_extractor.py`)
- **Frequency:** Every 6 hours
- **Purpose:** Extract winning/losing patterns from closed trades
- **Trigger:** Only runs when 20+ new closed trades since last analysis
- **Outputs:** 
  - `genius-memory/patterns/batch_NNN.json`
  - `state/pattern_extractor_state.json` (state tracking)
- **Notifications:** Telegram L2 with key findings

### 3. Prompt Optimizer (`prompt_optimizer.py`)
- **Frequency:** Weekly (Monday 3 AM UTC) OR every 50 trades
- **Purpose:** Propose prompt improvements based on pattern data and wrong predictions
- **Outputs:** 
  - `genius-memory/strategy-evolution/prompt_update_NNN.json` (proposals)
  - `genius-memory/strategy-evolution/prompt_versions/` (versioned prompts)
  - `state/prompt_optimizer_state.json` (state tracking)
- **Notifications:** Telegram L2 with diff
- **Manual Step:** Review and apply with `--apply NNN` flag

### 4. Regime Adapter (`regime_adapter.py`)
- **Frequency:** Every hour
- **Purpose:** Adapt trading behavior when market regime changes
- **Inputs:** 
  - `state/regime.json` (current regime, set by other components)
  - `config/regime_profiles.yaml` (regime configurations)
- **Outputs:** 
  - `state/active_regime_profile.json` (active profile for router/pipeline)
  - `state/regime_adapter_state.json` (state tracking)
- **Notifications:** Telegram L2 on regime change

## Log Files

Create the logs directory if it doesn't exist:

```bash
mkdir -p /data/.openclaw/workspace/trading/logs
```

Logs will be written to:
- `logs/daily_research.log`
- `logs/pattern_extractor.log`
- `logs/prompt_optimizer.log`
- `logs/regime_adapter.log`

## Testing

Before adding to cron, test each component:

```bash
cd /data/.openclaw/workspace/trading/scripts

# Test each component with --test flag (no API calls, no saves)
python3 daily_deep_research.py --test
python3 pattern_extractor.py --test
python3 prompt_optimizer.py --test
python3 regime_adapter.py --test

# Run smoke imports to verify no import regressions
python3 smoke_imports.py
```

## Integration with Existing System

### Router and Pipeline Integration

The `signal_router.py` and `sanad_pipeline.py` can now read regime adaptations:

```python
# Example: Read active regime profile in router/pipeline
import json
from pathlib import Path

active_profile_path = Path("state/active_regime_profile.json")
if active_profile_path.exists():
    with open(active_profile_path, "r") as f:
        active_profile = json.load(f)
    
    regime = active_profile.get("regime", "SIDEWAYS")
    profile = active_profile.get("profile", {})
    
    # Use profile settings
    sanad_trust_threshold = profile.get("pipeline_behavior", {}).get("sanad_trust_threshold", 0.7)
    strategy_weights = profile.get("strategy_weights", {})
    # ... apply adaptations
```

This integration is **not implemented in this commit** — the components only write `state/active_regime_profile.json`. The router and pipeline should be updated separately to read and apply these settings.

## Maintenance

- **Pattern batches:** Kept indefinitely in `genius-memory/patterns/`
- **Prompt versions:** Kept indefinitely in `genius-memory/strategy-evolution/prompt_versions/`
- **Regime history:** Last 50 regime changes in `state/regime_adapter_state.json`
- **Logs:** Rotate logs periodically (use `logrotate` or manual cleanup)

## Rollback

If any component causes issues:

1. **Disable cron:** Comment out the cron line
2. **Revert prompts:** `python3 prompt_optimizer.py --revert`
3. **Check logs:** Review `logs/*.log` for errors
4. **Git revert:** If needed, revert the component commit

## Summary

All 4 components are production-ready with:
- ✅ Smoke tests passing
- ✅ --test flag for dry runs
- ✅ Telegram L2 notifications
- ✅ State tracking for idempotency
- ✅ Error handling and logging
- ✅ Git commits with descriptive messages

Add the cron jobs above to activate the self-improvement loop.
