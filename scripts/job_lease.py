#!/usr/bin/env python3
"""
Job Lease System - Deterministic liveness tracking for cron jobs.

Provides proof-of-life independent of OpenClaw scheduler state.
Each job writes a lease file at start/end, giving watchdog a single source of truth.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Get SANAD_HOME from environment
SANAD_HOME = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
LEASE_DIR = SANAD_HOME / "state" / "leases"

def acquire(job_name: str, ttl_seconds: int) -> dict:
    """
    Acquire a job lease.
    
    Args:
        job_name: Unique job identifier (e.g., "signal_router", "coingecko_scanner")
        ttl_seconds: Expected maximum runtime (used by watchdog to detect stalls)
    
    Returns:
        Lease dict with started_at, pid, ttl
    """
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    
    lease = {
        "job_name": job_name,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": ttl_seconds,
        "status": "running"
    }
    
    lease_path = LEASE_DIR / f"{job_name}.json"
    with open(lease_path, "w") as f:
        json.dump(lease, f, indent=2)
    
    return lease

def heartbeat(job_name: str):
    """
    Update lease heartbeat timestamp.
    Call periodically during long-running jobs.
    """
    lease_path = LEASE_DIR / f"{job_name}.json"
    
    if not lease_path.exists():
        return
    
    try:
        with open(lease_path) as f:
            lease = json.load(f)
        
        lease["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        
        with open(lease_path, "w") as f:
            json.dump(lease, f, indent=2)
    except Exception:
        pass  # Don't crash job if heartbeat fails

def release(job_name: str, status: str = "ok", detail: Optional[str] = None):
    """
    Release job lease and record final status.
    
    Args:
        job_name: Job identifier
        status: "ok" | "error" | "timeout"
        detail: Optional error message or completion info
    """
    lease_path = LEASE_DIR / f"{job_name}.json"
    
    if not lease_path.exists():
        return
    
    try:
        with open(lease_path) as f:
            lease = json.load(f)
        
        lease["status"] = status
        lease["completed_at"] = datetime.now(timezone.utc).isoformat()
        if detail:
            lease["detail"] = detail
        
        # Write final state
        with open(lease_path, "w") as f:
            json.dump(lease, f, indent=2)
        
        # Optionally remove lease (or keep for last-run inspection)
        # For now, keep it so watchdog can see last completion time
    except Exception:
        pass

def check_lease(job_name: str) -> Optional[dict]:
    """
    Check if a job lease exists and return its contents.
    
    Returns:
        Lease dict if exists, None otherwise
    """
    lease_path = LEASE_DIR / f"{job_name}.json"
    
    if not lease_path.exists():
        return None
    
    try:
        with open(lease_path) as f:
            return json.load(f)
    except Exception:
        return None

def is_stale(job_name: str, grace_seconds: int = 60) -> bool:
    """
    Check if a job lease is stale (exceeded TTL + grace).
    
    Args:
        job_name: Job identifier
        grace_seconds: Additional grace period beyond TTL
    
    Returns:
        True if lease is stale or missing
    """
    lease = check_lease(job_name)
    
    if not lease:
        return True
    
    # If job completed, check completion freshness
    if lease.get("status") in ["ok", "error", "timeout"]:
        completed_at = lease.get("completed_at")
        if completed_at:
            completed_dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - completed_dt).total_seconds()
            # Completed jobs are "fresh" if they finished recently
            return age > grace_seconds
    
    # Job is running - check heartbeat + TTL
    heartbeat_at = lease.get("heartbeat_at", lease.get("started_at"))
    ttl = lease.get("ttl_seconds", 300)
    
    heartbeat_dt = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - heartbeat_dt).total_seconds()
    
    return age > (ttl + grace_seconds)

if __name__ == "__main__":
    # Test
    print("Testing job lease system...")
    
    lease = acquire("test_job", ttl_seconds=60)
    print(f"Acquired lease: {lease}")
    
    import time
    time.sleep(1)
    
    heartbeat("test_job")
    print("Heartbeat updated")
    
    release("test_job", "ok", "Test completed successfully")
    print("Lease released")
    
    lease = check_lease("test_job")
    print(f"Final lease state: {lease}")
    
    print(f"Is stale? {is_stale('test_job')}")
