#!/usr/bin/env python3
"""
Signal Queue — Sprint 3.8.2
Deterministic Python. No LLMs.

Queues incoming signals for orderly processing, prevents flooding.
- Max 5 signals queued at a time
- FIFO processing with priority override for CRITICAL signals
- Deduplication by token (same token within 10 min = skip)
- Rate limit: max 3 pipeline runs per hour (LLM budget protection)

Used by: signal_router, meme_radar, pumpfun_monitor, whale_tracker
Consumed by: sanad_pipeline.py (via cron or direct call)
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
QUEUE_PATH = STATE_DIR / "signal_queue.json"
LOGS_DIR = BASE_DIR / "execution-logs"

MAX_QUEUE_SIZE = 5
DEDUP_WINDOW_MIN = 10
MAX_PIPELINE_RUNS_PER_HOUR = 3
PRIORITIES = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[QUEUE] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _load_queue():
    try:
        with open(QUEUE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"queue": [], "processed": [], "pipeline_runs": []}


def _save_queue(data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, QUEUE_PATH)
    except Exception as e:
        _log(f"ERROR saving queue: {e}")


def enqueue(signal: dict, priority: str = "NORMAL") -> bool:
    """Add a signal to the queue.

    Args:
        signal: must have 'token' and 'source' fields minimum
        priority: CRITICAL, HIGH, NORMAL, LOW

    Returns:
        True if queued, False if rejected (duplicate, full, etc.)
    """
    now = _now()
    data = _load_queue()
    queue = data.get("queue", [])
    token = signal.get("token", signal.get("symbol", "UNKNOWN"))
    source = signal.get("source", "unknown")

    # 1. Queue size check
    if len(queue) >= MAX_QUEUE_SIZE and priority != "CRITICAL":
        _log(f"REJECT {token} from {source}: queue full ({len(queue)}/{MAX_QUEUE_SIZE})")
        return False

    # 2. Deduplication — same token within window
    cutoff = (now - timedelta(minutes=DEDUP_WINDOW_MIN)).isoformat()
    for item in queue:
        item_token = item.get("token", "")
        item_time = item.get("queued_at", "")
        if item_token.upper() == token.upper() and item_time > cutoff:
            _log(f"REJECT {token} from {source}: duplicate (already queued within {DEDUP_WINDOW_MIN}min)")
            return False

    # Also check recently processed
    processed = data.get("processed", [])
    for item in processed[-20:]:  # Check last 20 processed
        item_token = item.get("token", "")
        item_time = item.get("processed_at", "")
        if item_token.upper() == token.upper() and item_time > cutoff:
            _log(f"REJECT {token} from {source}: recently processed (within {DEDUP_WINDOW_MIN}min)")
            return False

    # 3. Build queue entry
    entry = {
        "token": token.upper(),
        "source": source,
        "priority": priority if priority in PRIORITIES else "NORMAL",
        "priority_rank": PRIORITIES.get(priority, 2),
        "signal": signal,
        "queued_at": now.isoformat(),
    }

    # 4. Insert sorted by priority (CRITICAL first)
    queue.append(entry)
    queue.sort(key=lambda x: x.get("priority_rank", 2))

    # 5. If over max, drop lowest priority
    if len(queue) > MAX_QUEUE_SIZE:
        dropped = queue.pop()
        _log(f"DROPPED {dropped['token']} (lowest priority) to make room")

    data["queue"] = queue
    _save_queue(data)
    _log(f"QUEUED {token} from {source} (priority={priority}, pos={len(queue)})")
    return True


def dequeue() -> dict | None:
    """Pop the highest-priority signal from the queue.

    Returns signal dict or None if empty/rate-limited.
    """
    now = _now()
    data = _load_queue()
    queue = data.get("queue", [])

    if not queue:
        return None

    # Rate limit check — max pipeline runs per hour
    runs = data.get("pipeline_runs", [])
    cutoff_1h = (now - timedelta(hours=1)).isoformat()
    recent_runs = [r for r in runs if r > cutoff_1h]

    if len(recent_runs) >= MAX_PIPELINE_RUNS_PER_HOUR:
        _log(f"RATE LIMITED: {len(recent_runs)}/{MAX_PIPELINE_RUNS_PER_HOUR} pipeline runs this hour")
        return None

    # Pop highest priority (first in sorted list)
    entry = queue.pop(0)

    # Record processing
    processed = data.get("processed", [])
    processed.append({
        "token": entry["token"],
        "source": entry["source"],
        "processed_at": now.isoformat(),
        "wait_time_s": (now - datetime.fromisoformat(entry["queued_at"])).total_seconds(),
    })

    # Keep only last 50 processed
    data["processed"] = processed[-50:]

    # Record pipeline run
    recent_runs.append(now.isoformat())
    data["pipeline_runs"] = recent_runs
    data["queue"] = queue
    _save_queue(data)

    _log(f"DEQUEUED {entry['token']} from {entry['source']} (waited {processed[-1]['wait_time_s']:.0f}s)")
    return entry["signal"]


def peek() -> list:
    """View queue contents without modifying."""
    data = _load_queue()
    return data.get("queue", [])


def clear():
    """Clear the entire queue."""
    _save_queue({"queue": [], "processed": [], "pipeline_runs": []})
    _log("Queue CLEARED")


def status() -> dict:
    """Get queue status."""
    now = _now()
    data = _load_queue()
    queue = data.get("queue", [])
    runs = data.get("pipeline_runs", [])
    cutoff_1h = (now - timedelta(hours=1)).isoformat()
    recent_runs = [r for r in runs if r > cutoff_1h]

    return {
        "depth": len(queue),
        "max_size": MAX_QUEUE_SIZE,
        "pipeline_runs_this_hour": len(recent_runs),
        "max_runs_per_hour": MAX_PIPELINE_RUNS_PER_HOUR,
        "rate_limited": len(recent_runs) >= MAX_PIPELINE_RUNS_PER_HOUR,
        "tokens_queued": [q["token"] for q in queue],
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        s = status()
        _log(f"Queue: {s['depth']}/{s['max_size']} | Runs: {s['pipeline_runs_this_hour']}/{s['max_runs_per_hour']} | Limited: {s['rate_limited']}")
        for q in peek():
            _log(f"  [{q['priority']}] {q['token']} from {q['source']} (queued {q['queued_at']})")
    elif len(sys.argv) > 1 and sys.argv[1] == "clear":
        clear()
    else:
        _log("=== SIGNAL QUEUE TEST ===")
        clear()

        # Test enqueue
        enqueue({"token": "PEPE", "source": "coingecko"}, "NORMAL")
        enqueue({"token": "WIF", "source": "dexscreener"}, "HIGH")
        enqueue({"token": "BONK", "source": "birdeye"}, "LOW")
        enqueue({"token": "PEPE", "source": "meme_radar"}, "NORMAL")  # Should be rejected (dup)
        enqueue({"token": "DOGE", "source": "coingecko"}, "CRITICAL")  # Should jump to front

        # Show queue
        _log(f"Queue: {[q['token'] for q in peek()]}")

        # Dequeue — should get CRITICAL first
        sig = dequeue()
        _log(f"Dequeued: {sig['token'] if sig else 'None'}")
        sig = dequeue()
        _log(f"Dequeued: {sig['token'] if sig else 'None'}")

        s = status()
        _log(f"Remaining: {s['depth']} | Runs: {s['pipeline_runs_this_hour']}")
        _log("✅ Signal queue working")
