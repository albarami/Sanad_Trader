#!/usr/bin/env python3
"""Provider sample capture (deterministic).

When PROVIDER_SAMPLES=1, callers can persist raw provider responses for
schema validation/debugging. Redaction is best-effort; do not store secrets.

Outputs:
  state/provider_samples/<provider>__<endpoint>__<ts>.json

"""

import json
import os
import time
from pathlib import Path

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
STATE_DIR = BASE_DIR / "state"
SAMPLES_DIR = STATE_DIR / "provider_samples"


def enabled() -> bool:
    return str(os.environ.get("PROVIDER_SAMPLES", "")).strip() in ("1", "true", "TRUE", "yes", "on")


def _safe_name(s: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in (s or ""))[:80]


def capture(provider: str, endpoint: str, payload) -> None:
    if not enabled():
        return
    try:
        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        path = SAMPLES_DIR / f"{_safe_name(provider)}__{_safe_name(endpoint)}__{ts}.json"
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
    except Exception:
        # best-effort only
        return
