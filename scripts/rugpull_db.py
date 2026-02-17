#!/usr/bin/env python3
"""
Rugpull Database Update — Sprint 6.1.16
Runs daily at 03:00 QAT (00:00 UTC).
Maintains a local database of known scam/rugpull contracts.

Sources:
- state/onchain_analytics_state.json (detected rugs)
- Existing rugpull_db.json
- Signal rejections tagged as rug/scam
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
RUGPULL_DB = STATE_DIR / "rugpull_db.json"
ONCHAIN_STATE = STATE_DIR / "onchain_analytics_state.json"
SIGNALS_DIR = BASE_DIR / "signals"


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[RUGDB] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def is_known_rug(contract_address: str) -> bool:
    """Check if a contract is in the rugpull database."""
    db = _load_json(RUGPULL_DB, {"contracts": {}})
    return contract_address.lower() in db.get("contracts", {})


def add_rug(contract_address: str, token: str = "", reason: str = "",
            source: str = "manual", chain: str = "unknown"):
    """Add a contract to the rugpull database."""
    db = _load_json(RUGPULL_DB, {"contracts": {}, "stats": {}})
    key = contract_address.lower()
    if key not in db["contracts"]:
        db["contracts"][key] = {
            "token": token,
            "reason": reason,
            "source": source,
            "chain": chain,
            "added_at": _now().isoformat(),
        }
        _save_json(RUGPULL_DB, db)
        return True
    return False


def update():
    """Update rugpull database from all sources."""
    _log("=== RUGPULL DATABASE UPDATE ===")

    db = _load_json(RUGPULL_DB, {"contracts": {}, "stats": {"total": 0, "last_update": ""}})
    initial_count = len(db.get("contracts", {}))

    # Source 1: On-chain analytics flagged rugs
    onchain = _load_json(ONCHAIN_STATE, {})
    for alert in onchain.get("rugpull_alerts", onchain.get("alerts", [])):
        addr = alert.get("contract", alert.get("address", ""))
        if addr:
            add_rug(addr,
                   token=alert.get("token", ""),
                   reason=alert.get("reason", "on-chain detection"),
                   source="onchain_analytics",
                   chain=alert.get("chain", "unknown"))

    # Source 2: Signal rejections tagged as scam
    if SIGNALS_DIR.exists():
        for subdir in SIGNALS_DIR.iterdir():
            if subdir.is_dir():
                for f in subdir.glob("*.json"):
                    try:
                        data = _load_json(f)
                        if isinstance(data, dict):
                            rejection = data.get("rejection_reason", "").lower()
                            if any(w in rejection for w in ["rug", "scam", "honeypot", "fraud"]):
                                addr = data.get("contract_address", data.get("contract", ""))
                                if addr:
                                    add_rug(addr,
                                           token=data.get("token", ""),
                                           reason=rejection,
                                           source="signal_rejection",
                                           chain=data.get("chain", "unknown"))
                    except Exception:
                        pass

    # Source 3: Telegram sniffer negative signals
    tg_state = _load_json(STATE_DIR / "telegram_sniffer_state.json", {})
    for neg in tg_state.get("negative_signals", []):
        addr = neg.get("contract", "")
        if addr:
            add_rug(addr,
                   token=neg.get("token", ""),
                   reason="telegram scam alert",
                   source="telegram_sniffer")

    # Update stats
    db = _load_json(RUGPULL_DB, {"contracts": {}, "stats": {}})
    final_count = len(db.get("contracts", {}))
    db["stats"]["total"] = final_count
    db["stats"]["last_update"] = _now().isoformat()
    db["stats"]["added_today"] = final_count - initial_count
    _save_json(RUGPULL_DB, db)

    _log(f"Database: {final_count} contracts ({final_count - initial_count} new today)")
    _log("=== UPDATE COMPLETE ===")
    return db


if __name__ == "__main__":
    update()
