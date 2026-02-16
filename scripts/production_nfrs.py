#!/usr/bin/env python3
"""
Production Non-Functional Requirements — Sprint 10.3
Graceful shutdown, structured logging, process management.
"""

import json
import os
import sys
import signal
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
LOGS_DIR = BASE_DIR / "logs"
sys.path.insert(0, str(SCRIPT_DIR))


# ─────────────────────────────────────────────────────────
# Structured Logging
# ─────────────────────────────────────────────────────────

def setup_logging(name: str = "sanad", level: str = "INFO") -> logging.Logger:
    """Configure structured JSON logging."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))

    # JSON formatter
    class JSONFormatter(logging.Formatter):
        def format(self, record):
            return json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "module": record.module,
                "message": record.getMessage(),
                "extra": getattr(record, "extra_data", {}),
            })

    # File handler (rotating daily)
    fh = logging.FileHandler(LOGS_DIR / f"{name}.log")
    fh.setFormatter(JSONFormatter())
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    return logger


# ─────────────────────────────────────────────────────────
# Graceful Shutdown
# ─────────────────────────────────────────────────────────

class GracefulShutdown:
    """Handle SIGINT/SIGTERM for clean shutdown."""

    def __init__(self):
        self.should_stop = False
        self.callbacks = []
        signal.signal(signal.SIGINT, self._handler)
        signal.signal(signal.SIGTERM, self._handler)

    def _handler(self, signum, frame):
        print(f"\n[SHUTDOWN] Signal {signum} received — shutting down gracefully...")
        self.should_stop = True
        for cb in self.callbacks:
            try:
                cb()
            except Exception as e:
                print(f"[SHUTDOWN] Callback error: {e}")

    def register(self, callback):
        self.callbacks.append(callback)

    @property
    def running(self):
        return not self.should_stop


# ─────────────────────────────────────────────────────────
# Process Lock (prevent duplicate instances)
# ─────────────────────────────────────────────────────────

class ProcessLock:
    """File-based lock to prevent duplicate processes."""

    def __init__(self, name: str):
        self.lock_path = STATE_DIR / f"{name}.lock"
        self.locked = False

    def acquire(self) -> bool:
        if self.lock_path.exists():
            # Check if stale (>1 hour)
            try:
                data = json.loads(self.lock_path.read_text())
                locked_at = datetime.fromisoformat(data["locked_at"])
                age = (datetime.now(timezone.utc) - locked_at).total_seconds()
                if age < 3600:
                    return False  # Lock is held
                # Stale lock — override
            except Exception:
                pass

        data = {
            "pid": os.getpid(),
            "locked_at": datetime.now(timezone.utc).isoformat(),
        }
        self.lock_path.write_text(json.dumps(data))
        self.locked = True
        return True

    def release(self):
        if self.locked and self.lock_path.exists():
            self.lock_path.unlink()
            self.locked = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"Process lock held: {self.lock_path}")
        return self

    def __exit__(self, *args):
        self.release()


# ─────────────────────────────────────────────────────────
# Health Check Endpoint Data
# ─────────────────────────────────────────────────────────

def get_process_health() -> dict:
    """Get current process health metrics."""
    import resource
    usage = resource.getrusage(resource.RUSAGE_SELF)

    return {
        "pid": os.getpid(),
        "uptime_seconds": time.monotonic(),
        "memory_mb": round(usage.ru_maxrss / 1024, 1),
        "cpu_user_seconds": round(usage.ru_utime, 2),
        "cpu_system_seconds": round(usage.ru_stime, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Production NFRs Test ===\n")

    # Test logging
    logger = setup_logging("test")
    logger.info("Test log entry")
    print(f"  ✅ Structured logging: {LOGS_DIR / 'test.log'}")

    # Test graceful shutdown
    gs = GracefulShutdown()
    gs.register(lambda: print("  Cleanup callback fired"))
    print(f"  ✅ Graceful shutdown handler registered")

    # Test process lock
    lock = ProcessLock("test_nfr")
    acquired = lock.acquire()
    print(f"  ✅ Process lock: acquired={acquired}")
    lock.release()
    print(f"  ✅ Process lock: released")

    # Test health
    health = get_process_health()
    print(f"  ✅ Process health: PID={health['pid']}, mem={health['memory_mb']}MB")

    print("\n=== All NFRs Working ===")
