#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Consume Safe Mode Recovery Slot

Call this script (or equivalent inline logic) only when a sync pre-trade
cold path returns APPROVE during RECOVERY mode.

Each call decrements recovery_remaining by 1. When it reaches 0, the
safe_mode.flag is removed and normal trading resumes.

Intended integration point: signal_router.py, after a sync cold path
APPROVE during RECOVERY mode, before calling fast_decision_engine.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
FLAG = BASE_DIR / "config" / "safe_mode.flag"


def consume_recovery_slot() -> dict:
    """
    Decrement recovery_remaining by 1.

    Returns: {"status": "consumed"|"complete"|"not_in_recovery"|"no_flag",
              "remaining": int or None}
    """
    if not FLAG.exists():
        print("No safe_mode.flag present.")
        return {"status": "no_flag", "remaining": None}

    data = json.loads(FLAG.read_text())
    mode = (data.get("mode") or "ACTIVE").upper()

    if mode != "RECOVERY":
        print(f"safe_mode.flag mode={mode} (not RECOVERY); nothing to consume.")
        return {"status": "not_in_recovery", "remaining": None}

    remaining = int(data.get("recovery_remaining", 0))
    required = int(data.get("recovery_required", remaining))

    if remaining <= 0:
        print("Recovery already complete; removing flag.")
        FLAG.unlink()
        return {"status": "complete", "remaining": 0}

    remaining -= 1
    data["recovery_remaining"] = remaining
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    FLAG.write_text(json.dumps(data, indent=2))
    print(f"Consumed 1 recovery slot → remaining {remaining}/{required}")

    if remaining <= 0:
        print("Recovery complete → removing flag (CLEAR).")
        FLAG.unlink()
        return {"status": "complete", "remaining": 0}

    return {"status": "consumed", "remaining": remaining}


def main():
    result = consume_recovery_slot()
    print(f"Result: {result}")


if __name__ == "__main__":
    main()
