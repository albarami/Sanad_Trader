#!/usr/bin/env python3
"""
Rejection Funnel Telemetry — Phase 1
Tracks where signals die in the pipeline. Resets daily.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BASE_DIR / "state"
FUNNEL_PATH = STATE_DIR / "rejection_funnel.json"

EMPTY_FUNNEL = {
    "date": "",
    "signals_ingested": 0,
    "pre_sanad_rejected": 0,
    "sanad_blocked": 0,
    "meme_gate_blocked": 0,
    "judge_rejected": 0,
    "judge_approved": 0,
    "judge_revised": 0,
    "policy_blocked": 0,
    "policy_blocked_gates": {},
    "executed": 0,
    "fast_tracked": 0,
    "short_circuited": 0,
}


def _load_funnel():
    try:
        with open(FUNNEL_PATH) as f:
            data = json.load(f)
        # Reset if new day
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if data.get("date") != today:
            data = dict(EMPTY_FUNNEL)
            data["date"] = today
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        data = dict(EMPTY_FUNNEL)
        data["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return data


def _save_funnel(data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FUNNEL_PATH.with_suffix(f".tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, FUNNEL_PATH)


def increment(field: str, gate_name: str = None):
    """Increment a funnel counter. Thread-safe via atomic write."""
    data = _load_funnel()
    if field in data and isinstance(data[field], int):
        data[field] += 1
    if gate_name and field == "policy_blocked":
        gates = data.get("policy_blocked_gates", {})
        gates[gate_name] = gates.get(gate_name, 0) + 1
        data["policy_blocked_gates"] = gates
    _save_funnel(data)


def get_funnel() -> dict:
    return _load_funnel()


def format_report() -> str:
    d = _load_funnel()
    total = d["signals_ingested"] or 1
    lines = [
        f"📊 Rejection Funnel — {d['date']}",
        f"Signals ingested: {d['signals_ingested']}",
        f"├─ Pre-Sanad rejected: {d['pre_sanad_rejected']}",
        f"├─ Short-circuited (BLOCK): {d['short_circuited']}",
        f"├─ Sanad blocked (<threshold): {d['sanad_blocked']}",
        f"├─ Meme gate blocked: {d['meme_gate_blocked']}",
        f"├─ Judge REJECTED: {d['judge_rejected']}",
        f"├─ Judge REVISED: {d['judge_revised']}",
        f"├─ Judge APPROVED: {d['judge_approved']}",
        f"├─ Policy blocked: {d['policy_blocked']}",
    ]
    if d.get("policy_blocked_gates"):
        for gate, count in sorted(d["policy_blocked_gates"].items(), key=lambda x: -x[1]):
            lines.append(f"│   └─ {gate}: {count}")
    lines.extend([
        f"├─ Fast-tracked: {d['fast_tracked']}",
        f"└─ EXECUTED: {d['executed']}",
        f"",
        f"Pass rate: {(d['executed'] + d['fast_tracked']) / total * 100:.1f}%",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report())
