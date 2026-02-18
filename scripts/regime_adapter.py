#!/usr/bin/env python3
"""
Regime Adapter — Self-Improvement Component 4

Adapts trading system behavior based on current market regime.
Loads regime profiles from config/regime_profiles.yaml and applies them when regime changes.

Regimes: EXTREME_FEAR, BEAR_HIGH_VOL, BEAR_LOW_VOL, BULL_TREND, BULL_HIGH_VOL, SIDEWAYS

Process:
1. Load current regime from state/regime.json
2. Load matching profile from config/regime_profiles.yaml
3. Write active profile to state/active_regime_profile.json
4. Telegram notification on regime change
5. Router and pipeline read active profile (DO NOT modify those files)

Usage:
    python3 regime_adapter.py          # Check regime and adapt
    python3 regime_adapter.py --test   # Dry run (no save, no Telegram)
    python3 regime_adapter.py --force  # Force reapply current regime
"""

import os
import sys
import json
import argparse
import yaml
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────
# Environment Setup
# ─────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
SCRIPTS_DIR = BASE_DIR / "scripts"
CONFIG_DIR = BASE_DIR / "config"
STATE_DIR = BASE_DIR / "state"

sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import notifier
    HAS_NOTIFIER = True
except ImportError:
    HAS_NOTIFIER = False

REGIME_FILE = STATE_DIR / "regime.json"
ACTIVE_PROFILE_FILE = STATE_DIR / "active_regime_profile.json"
PROFILES_CONFIG = CONFIG_DIR / "regime_profiles.yaml"
ADAPTER_STATE_FILE = STATE_DIR / "regime_adapter_state.json"

# ─────────────────────────────────────────────
# State Management
# ─────────────────────────────────────────────

def load_current_regime() -> str:
    """Load current regime from state/regime.json."""
    if not REGIME_FILE.exists():
        print(f"⚠️  No regime.json found, defaulting to SIDEWAYS")
        return "SIDEWAYS"
    
    try:
        with open(REGIME_FILE, "r") as f:
            data = json.load(f)
        
        # Handle different formats
        if isinstance(data, dict):
            regime = data.get("current_regime", data.get("regime", "SIDEWAYS"))
        else:
            regime = "SIDEWAYS"
        
        return regime.upper()
    except Exception as e:
        print(f"⚠️  Error reading regime.json: {e}, defaulting to SIDEWAYS")
        return "SIDEWAYS"


def load_regime_profiles() -> dict:
    """Load regime profiles from config/regime_profiles.yaml."""
    if not PROFILES_CONFIG.exists():
        raise FileNotFoundError(f"Regime profiles config not found: {PROFILES_CONFIG}")
    
    with open(PROFILES_CONFIG, "r") as f:
        profiles = yaml.safe_load(f)
    
    return profiles


def load_adapter_state() -> dict:
    """Load regime adapter state."""
    if ADAPTER_STATE_FILE.exists():
        with open(ADAPTER_STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "last_regime": None,
        "last_change_timestamp": None,
        "regime_history": []
    }


def save_adapter_state(state: dict):
    """Save regime adapter state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ADAPTER_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, ADAPTER_STATE_FILE)


def save_active_profile(regime: str, profile: dict, test_mode: bool = False):
    """Save active regime profile to state/active_regime_profile.json."""
    if test_mode:
        print(f"    [TEST MODE] Would save active profile for {regime}")
        return
    
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    
    active_profile = {
        "regime": regime,
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile
    }
    
    tmp = ACTIVE_PROFILE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(active_profile, f, indent=2)
    os.replace(tmp, ACTIVE_PROFILE_FILE)
    
    print(f"💾 Active profile saved: {ACTIVE_PROFILE_FILE}")


# ─────────────────────────────────────────────
# Regime Adaptation
# ─────────────────────────────────────────────

def adapt_to_regime(force: bool = False, test_mode: bool = False):
    """Main regime adaptation logic."""
    print(f"{'='*60}")
    print(f"Regime Adapter — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}\n")
    
    if test_mode:
        print("⚠️  TEST MODE: No file saves, no Telegram\n")
    
    # Load current regime
    print(f"📊 Loading current regime...")
    current_regime = load_current_regime()
    print(f"    Current regime: {current_regime}\n")
    
    # Load adapter state
    state = load_adapter_state()
    last_regime = state.get("last_regime")
    
    # Check if regime changed
    regime_changed = (current_regime != last_regime)
    
    if not regime_changed and not force:
        print(f"✅ Regime unchanged ({current_regime})")
        print(f"   No adaptation needed. Use --force to reapply.\n")
        return
    
    if force:
        print(f"🔧 Force reapplying regime: {current_regime}\n")
    else:
        print(f"🔄 Regime changed: {last_regime} → {current_regime}\n")
    
    # Load profiles
    print(f"📂 Loading regime profiles...")
    try:
        profiles = load_regime_profiles()
        print(f"    Loaded {len(profiles)} profiles\n")
    except Exception as e:
        print(f"❌ Failed to load profiles: {e}")
        raise
    
    # Get profile for current regime
    if current_regime not in profiles:
        print(f"⚠️  No profile found for {current_regime}, using SIDEWAYS")
        current_regime = "SIDEWAYS"
    
    profile = profiles[current_regime]
    
    # Display profile summary
    print(f"📋 Profile: {current_regime}")
    print(f"   Description: {profile.get('description', 'N/A')}")
    
    if 'strategy_weights' in profile:
        print(f"   Strategy weights:")
        for strategy, weight in profile['strategy_weights'].items():
            print(f"      {strategy}: {weight}")
    
    if 'position_sizing' in profile:
        sizing = profile['position_sizing']
        print(f"   Position sizing:")
        print(f"      Base: {sizing.get('base_position_pct', 'N/A')}%")
        print(f"      Max: {sizing.get('max_position_pct', 'N/A')}%")
        print(f"      Total exposure: {sizing.get('max_total_exposure', 'N/A')}%")
    
    print()
    
    # Save active profile
    save_active_profile(current_regime, profile, test_mode)
    
    # Update adapter state
    if not test_mode:
        state["last_regime"] = current_regime
        state["last_change_timestamp"] = datetime.now(timezone.utc).isoformat()
        
        # Add to history
        if "regime_history" not in state:
            state["regime_history"] = []
        state["regime_history"].append({
            "regime": current_regime,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        
        # Keep last 50 regime changes
        if len(state["regime_history"]) > 50:
            state["regime_history"] = state["regime_history"][-50:]
        
        save_adapter_state(state)
        print(f"💾 Adapter state updated")
    
    # Send Telegram notification on regime change
    if regime_changed and not test_mode:
        if HAS_NOTIFIER:
            msg = _build_notification(current_regime, last_regime, profile)
            try:
                notifier.send(msg, level='L2')
                print(f"📱 Telegram notification sent")
            except Exception as e:
                print(f"⚠️  Telegram send failed: {e}")
    
    print(f"\n{'='*60}")
    print(f"✅ Regime adaptation complete")
    print(f"{'='*60}")


def _build_notification(new_regime: str, old_regime: str, profile: dict) -> str:
    """Build Telegram notification for regime change."""
    msg = f"*Regime Change Detected*\n\n"
    
    if old_regime:
        msg += f"📉 Previous: {old_regime}\n"
    msg += f"📊 Current: {new_regime}\n\n"
    
    description = profile.get('description', 'N/A')
    msg += f"_{description}_\n\n"
    
    # Key adaptations
    msg += "*Key Adaptations:*\n"
    
    # Strategy weights
    if 'strategy_weights' in profile:
        weights = profile['strategy_weights']
        top_strategy = max(weights.items(), key=lambda x: x[1])
        msg += f"• Primary strategy: {top_strategy[0]} ({int(top_strategy[1]*100)}%)\n"
    
    # Position sizing
    if 'position_sizing' in profile:
        sizing = profile['position_sizing']
        base = sizing.get('base_position_pct', 'N/A')
        exposure = sizing.get('max_total_exposure', 'N/A')
        msg += f"• Position size: {base}% (max exposure: {exposure}%)\n"
    
    # Pipeline behavior
    if 'pipeline_behavior' in profile:
        behavior = profile['pipeline_behavior']
        trust = behavior.get('sanad_trust_threshold', 'N/A')
        conf = behavior.get('muhasbi_min_confidence', 'N/A')
        msg += f"• Trust threshold: {trust} (confidence: {conf})\n"
    
    return msg


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Regime Adapter")
    parser.add_argument("--test", action="store_true", help="Dry run (no saves, no Telegram)")
    parser.add_argument("--force", action="store_true", help="Force reapply current regime")
    args = parser.parse_args()
    
    try:
        adapt_to_regime(force=args.force, test_mode=args.test)
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
