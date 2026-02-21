#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Canonical ID Generation (FINAL)
Full-length hashes. Content fingerprints. Stable across enrichment cycles.
"""

import hashlib
import re
from datetime import datetime, timezone


def normalize_text(text: str) -> str:
    """Normalize text for fingerprinting."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def make_signal_id(signal: dict) -> str:
    """
    Generate deterministic signal_id from stable content.
    
    Priority:
    1. Event ID (if available) - most stable
    2. Content fingerprint (chain + token + source + type + thesis)
    3. Fallback: add 10-min time bucket if thesis too sparse
    
    Does NOT include: rugcheck_score, volume_24h, or other computed metrics.
    Those change across enrichment cycles and break idempotency.
    
    Returns: Full 64-char hex (256-bit SHA256)
    """
    # Priority 1: Event ID if available
    event_id = signal.get("source_event_id") or signal.get("message_id")
    if event_id:
        composite = f"event|{event_id}"
        return hashlib.sha256(composite.encode()).hexdigest()
    
    # Priority 2: Content fingerprint (stable fields only)
    chain = signal.get("chain", "unknown")
    token = signal.get("token_address") or signal.get("token", "unknown")
    source = signal.get("source_primary") or signal.get("source", "unknown")
    sig_type = signal.get("signal_type", "generic")
    thesis = normalize_text(signal.get("thesis", ""))
    
    # Base fingerprint (NO rugcheck, NO volume)
    composite = f"{chain}|{token}|{source}|{sig_type}|{thesis}"
    
    # Fallback: add time bucket if content too sparse
    if len(thesis) < 10:
        ts_str = signal.get("timestamp")
        if ts_str:
            try:
                # Safe timestamp parsing
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                bucket = int(ts.timestamp() // 600) * 600  # 10min bucket
                bucket_str = datetime.fromtimestamp(bucket, tz=timezone.utc).isoformat()
                composite += f"|{bucket_str}"
            except (ValueError, TypeError, AttributeError):
                # Malformed timestamp: use current time bucket as fallback
                now_bucket = int(datetime.now(timezone.utc).timestamp() // 600) * 600
                bucket_str = datetime.fromtimestamp(now_bucket, tz=timezone.utc).isoformat()
                composite += f"|{bucket_str}_fallback"
    
    return hashlib.sha256(composite.encode()).hexdigest()


def make_decision_id(signal_id: str, policy_version: str) -> str:
    """
    decision_id = sha256(signal_id + policy_version)
    Full 64-char hex.
    """
    composite = f"{signal_id}|{policy_version}"
    return hashlib.sha256(composite.encode()).hexdigest()


def make_position_id(decision_id: str, execution_ordinal=1) -> str:
    """
    position_id = sha256(decision_id + execution_ordinal)
    Full 64-char hex.
    """
    composite = f"{decision_id}|{execution_ordinal}"
    return hashlib.sha256(composite.encode()).hexdigest()
