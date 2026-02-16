#!/usr/bin/env python3
"""
Environment Loader — Shared utility
Loads API keys from .env file into os.environ.
Checks multiple possible locations.
"""

import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent

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
                        if key and value and key not in os.environ:
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


# Auto-load on import
load_env()
