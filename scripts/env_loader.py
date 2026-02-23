#!/usr/bin/env python3
"""
Environment Loader — Shared utility
Loads API keys from .env file into os.environ.
Checks multiple possible locations.
"""

import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# SANAD_HOME: canonical base directory. Env var takes priority over __file__ inference.
BASE_DIR = Path(os.environ.get("SANAD_HOME", "")) if os.environ.get("SANAD_HOME") else SCRIPT_DIR.parent
SANAD_HOME = BASE_DIR
STATE_DIR = BASE_DIR / "state"

ENV_PATHS = [
    BASE_DIR / ".env",
    BASE_DIR / "config" / ".env",
    Path("/data/.openclaw/.env"),
    Path("/data/.openclaw/workspace/.env"),
    Path.home() / ".env",
]


def load_env():
    """Load .env file into os.environ."""
    for env_path in ENV_PATHS:
        if env_path.exists():
            try:
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        # Override empty-string env vars (common in cron/service shells)
                        # but do not overwrite non-empty values.
                        if key and value and (key not in os.environ or os.environ.get(key, "") == ""):
                            os.environ[key] = value
            except Exception:
                pass


def get_key(key_name: str) -> str | None:
    """Get an API key, loading .env if needed."""
    val = os.environ.get(key_name)
    if val:
        return val
    load_env()
    return os.environ.get(key_name)


def get_base_dir() -> Path:
    """Return SANAD_HOME (canonical base directory). Use this instead of Path(__file__).resolve().parent.parent."""
    return BASE_DIR


def get_state_dir() -> Path:
    """Return state directory."""
    return STATE_DIR


# Auto-load on import
load_env()
# Re-resolve BASE_DIR after env is loaded (SANAD_HOME may now be set)
_sanad_home_env = os.environ.get("SANAD_HOME")
if _sanad_home_env:
    BASE_DIR = Path(_sanad_home_env)
    SANAD_HOME = BASE_DIR
    STATE_DIR = BASE_DIR / "state"
