"""
Supabase Client Utility — Sanad Trader v3.0

Handles all communication with Supabase for event logging,
system status sync, and real-time dashboard data.

Uses service role key for server-side operations.
Credentials loaded from trading/config/.env (gitignored).
"""

import os
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

# Load .env from config directory
BASE_DIR = Path("/data/.openclaw/workspace/trading")
ENV_PATH = BASE_DIR / "config" / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except PermissionError:
    print("[DOTENV] Permission denied reading .env — skipping")
except Exception as e:
    print(f"[DOTENV] Error loading .env: {e}")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

_client = None


def get_client():
    """Get or create Supabase client (lazy singleton)."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "Supabase credentials not found. "
                "Ensure trading/config/.env exists with SUPABASE_URL and SUPABASE_SERVICE_KEY."
            )
        from supabase import create_client
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def compute_event_hash(event_type, payload, prev_hash="GENESIS"):
    """Compute SHA-256 hash for event chain integrity."""
    content = f"{event_type}|{json.dumps(payload, sort_keys=True)}|{prev_hash}"
    return hashlib.sha256(content.encode()).hexdigest()


def get_last_event_hash():
    """Retrieve the hash of the most recent event for chain continuity."""
    try:
        client = get_client()
        result = (
            client.table("events")
            .select("prev_event_hash")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if result.data and len(result.data) > 0:
            return result.data[0].get("prev_event_hash", "GENESIS")
    except Exception as e:
        print(f"[SUPABASE] Warning: Could not fetch last event hash: {e}")
    return "GENESIS"


def log_event(event_type, payload, correlation_id=None):
    """
    Log an event to the Supabase events table.
    Events are hash-chained for integrity (v3 doc requirement).
    DB auto-generates created_at timestamp.
    """
    try:
        client = get_client()
        prev_hash = get_last_event_hash()
        event_hash = compute_event_hash(event_type, payload, prev_hash)

        record = {
            "event_type": event_type,
            "payload": payload,
            "prev_event_hash": event_hash,
        }
        if correlation_id:
            record["correlation_id"] = correlation_id

        result = client.table("events").insert(record).execute()

        if result.data:
            print(f"[SUPABASE] Event logged: {event_type} (id={result.data[0].get('id')})")
            return result.data[0]
        else:
            print(f"[SUPABASE] Warning: Event insert returned no data")
            return None

    except Exception as e:
        print(f"[SUPABASE] Error logging event: {e}")
        return None


def sync_system_status(status_data):
    """
    Upsert system status to Supabase system_status table.
    Called by heartbeat to keep dashboard current.
    """
    try:
        client = get_client()

        record = {
            "component": status_data.get("component", "heartbeat"),
            "mode": status_data.get("mode", "PAPER"),
            "kill_switch": status_data.get("kill_switch", False),
            "heartbeat_last": datetime.now(timezone.utc).isoformat(),
            "cron_health": status_data.get("cron_health", {}),
            "api_errors": status_data.get("api_errors", {}),
            "budget_used": status_data.get("budget_used", 0),
        }

        result = (
            client.table("system_status")
            .upsert(record, on_conflict="component")
            .execute()
        )

        if result.data:
            print(f"[SUPABASE] System status synced: {record['component']}")
            return result.data[0]

    except Exception as e:
        print(f"[SUPABASE] Error syncing system status: {e}")
        return None


def sync_circuit_breaker(component_name, state, failure_count=0, cooldown_until=None):
    """Upsert circuit breaker state to Supabase."""
    try:
        client = get_client()

        record = {
            "component_name": component_name,
            "state": state,
            "failure_count": failure_count,
            "cooldown_until": cooldown_until,
        }

        result = (
            client.table("circuit_breakers")
            .upsert(record, on_conflict="component_name")
            .execute()
        )

        if result.data:
            print(f"[SUPABASE] Circuit breaker synced: {component_name} = {state}")
            return result.data[0]

    except Exception as e:
        print(f"[SUPABASE] Error syncing circuit breaker: {e}")
        return None


def test_connection():
    """Test Supabase connectivity with a real event insert."""
    try:
        client = get_client()

        # Read test
        result = client.table("events").select("id", count="exact").limit(1).execute()
        event_count = result.count if hasattr(result, 'count') else 0
        print(f"[SUPABASE] Connection OK. Events table has {event_count} records.")

        # Write test
        test_event = log_event(
            "SYSTEM_ERROR",
            {
                "component": "supabase_client",
                "error_type": "connectivity_test",
                "error_message": "Supabase integration test — not a real error",
                "impact": "none",
                "auto_response_taken": "none"
            }
        )

        if test_event:
            print(f"[SUPABASE] Test event logged. ID: {test_event.get('id')}")
            return True
        else:
            print("[SUPABASE] Warning: Connection works but event insert failed.")
            return False

    except Exception as e:
        print(f"[SUPABASE] Connection FAILED: {e}")
        return False


if __name__ == "__main__":
    print("=" * 50)
    print("Supabase Client — Connection Test")
    print("=" * 50)
    print(f"URL: {SUPABASE_URL}")
    print(f"Key loaded: {'Yes' if SUPABASE_KEY else 'NO — MISSING'}")
    print()

    success = test_connection()
    print()
    print(f"Result: {'PASS' if success else 'FAIL'}")
    exit(0 if success else 1)
