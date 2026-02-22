#!/usr/bin/env python3
"""
Reset Safe Mode Recovery Counter

Temporary utility to manually reset sync_cold_path_required counter
until pre-trade cold path validation is implemented.

Usage:
  python3 reset_safe_mode_recovery.py

This removes the safe_mode.flag file entirely, allowing normal trading to resume.

CAUTION: Only use this after verifying that:
1. Quality issues have been resolved (no recent catastrophic rejects)
2. Universal gates (stablecoin, holder, self-pair) are functioning
3. Learning loop has processed all recent closures with new penalties
"""

import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path("/data/.openclaw/workspace/trading")
SAFE_MODE_FLAG = BASE_DIR / "config" / "safe_mode.flag"

def main():
    if not SAFE_MODE_FLAG.exists():
        print("✅ No safe mode flag present. System is already in normal operation.")
        return
    
    try:
        flag_data = json.loads(SAFE_MODE_FLAG.read_text())
        print(f"Safe mode flag found:")
        print(f"  Activated: {flag_data.get('activated_at')}")
        print(f"  Expires: {flag_data.get('expires_at')}")
        print(f"  Reason: {flag_data.get('reason')}")
        print(f"  Sync required: {flag_data.get('sync_cold_path_required', 0)}")
        print(f"  Stats: {flag_data.get('stats')}")
        print()
        
        response = input("Remove safe mode flag and resume normal trading? (yes/no): ")
        if response.lower() == "yes":
            SAFE_MODE_FLAG.unlink()
            print("✅ Safe mode flag removed. System will resume normal trading on next router run.")
        else:
            print("❌ Aborted. Safe mode remains active.")
    except Exception as e:
        print(f"ERROR reading safe mode flag: {e}")

if __name__ == "__main__":
    main()
