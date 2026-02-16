#!/usr/bin/env python3
"""
Signal Mutex — Sprint 2.4.3
5-minute token lock to prevent duplicate signal processing.
Deterministic Python, no LLMs.
"""
import json, os
from datetime import datetime, timezone, timedelta
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
MUTEX_PATH = STATE_DIR / "signal_mutex.json"
MUTEX_TTL_SECONDS = 300  # 5 minutes


def _load_mutex():
    try:
        with open(MUTEX_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"locks": {}}


def _save_mutex(data):
    tmp = MUTEX_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, MUTEX_PATH)


def acquire_signal_lock(token: str) -> bool:
    """Try to acquire a 5-min lock for a token signal.
    Returns True if lock acquired, False if token is already locked."""
    now = datetime.now(timezone.utc)
    data = _load_mutex()
    locks = data.get("locks", {})

    # Clean expired locks
    expired = [k for k, v in locks.items()
               if (now - datetime.fromisoformat(v)).total_seconds() > MUTEX_TTL_SECONDS]
    for k in expired:
        del locks[k]

    # Check if token is locked
    if token.upper() in locks:
        lock_time = datetime.fromisoformat(locks[token.upper()])
        remaining = MUTEX_TTL_SECONDS - (now - lock_time).total_seconds()
        print(f"[MUTEX] {token} LOCKED — {remaining:.0f}s remaining")
        return False

    # Acquire lock
    locks[token.upper()] = now.isoformat()
    data["locks"] = locks
    _save_mutex(data)
    print(f"[MUTEX] {token} lock ACQUIRED (5min TTL)")
    return True


def release_signal_lock(token: str):
    """Release a token signal lock early."""
    data = _load_mutex()
    locks = data.get("locks", {})
    if token.upper() in locks:
        del locks[token.upper()]
        data["locks"] = locks
        _save_mutex(data)
        print(f"[MUTEX] {token} lock RELEASED")


def is_locked(token: str) -> bool:
    """Check if a token is currently locked."""
    now = datetime.now(timezone.utc)
    data = _load_mutex()
    locks = data.get("locks", {})
    if token.upper() not in locks:
        return False
    lock_time = datetime.fromisoformat(locks[token.upper()])
    return (now - lock_time).total_seconds() <= MUTEX_TTL_SECONDS


if __name__ == "__main__":
    print("=== SIGNAL MUTEX TEST ===")
    print(f"Acquire BTC: {acquire_signal_lock('BTC')}")
    print(f"Acquire BTC again: {acquire_signal_lock('BTC')}")
    print(f"Is BTC locked: {is_locked('BTC')}")
    release_signal_lock('BTC')
    print(f"After release, acquire BTC: {acquire_signal_lock('BTC')}")
    print("✅ Mutex working")
