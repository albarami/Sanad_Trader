#!/usr/bin/env python3
"""
Model Upgrade Check — Sprint 6.1.19
Runs Monday 06:00 QAT (03:00 UTC).
Checks for new model releases from Anthropic + OpenAI.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[MODEL] {ts} {msg}", flush=True)


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


# Models currently used by Sanad Trader
CURRENT_MODELS = {
    "anthropic": {
        "opus": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-5-20250929",
    },
    "openai": {
        "gpt5_codex": "gpt-5.2-chat-latest",
    },
}


def check():
    _log("=== MODEL UPGRADE CHECK ===")

    import env_loader

    state_path = STATE_DIR / "model_check_state.json"
    state = _load_json(state_path, {"last_check": "", "known_models": {}})

    # Check Anthropic models
    _log("Checking Anthropic models...")
    api_key = env_loader.get_key("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import requests
            # List models isn't a standard endpoint, so we check our known models
            # by attempting a minimal call
            for name, model_id in CURRENT_MODELS["anthropic"].items():
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model_id,
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                    timeout=15,
                )
                status = "AVAILABLE" if resp.status_code == 200 else f"ERROR_{resp.status_code}"
                state["known_models"][model_id] = {
                    "status": status,
                    "checked_at": _now().isoformat(),
                }
                _log(f"  {name} ({model_id}): {status}")
        except Exception as e:
            _log(f"  Anthropic check failed: {e}")

    # Check OpenAI models
    _log("Checking OpenAI models...")
    oai_key = env_loader.get_key("OPENAI_API_KEY")
    if oai_key:
        try:
            import requests
            resp = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {oai_key}"},
                timeout=15,
            )
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                model_ids = [m["id"] for m in models]

                # Check for new GPT models
                gpt_models = sorted([m for m in model_ids if "gpt-5" in m.lower() or "gpt-4" in m.lower()])
                _log(f"  Available GPT-4+ models: {len(gpt_models)}")

                # Check if our current models are still available
                for name, model_id in CURRENT_MODELS["openai"].items():
                    available = model_id in model_ids
                    state["known_models"][model_id] = {
                        "status": "AVAILABLE" if available else "NOT_FOUND",
                        "checked_at": _now().isoformat(),
                    }
                    _log(f"  {name} ({model_id}): {'AVAILABLE' if available else 'NOT_FOUND'}")

                # Flag any new models
                state["available_gpt4_models"] = gpt_models[-5:] if gpt_models else []
        except Exception as e:
            _log(f"  OpenAI check failed: {e}")

    state["last_check"] = _now().isoformat()
    _save_json(state_path, state)

    # Alert if any model is unavailable
    unavailable = [m for m, info in state.get("known_models", {}).items()
                   if info.get("status") not in ("AVAILABLE",)]
    if unavailable:
        try:
            import notifier
            msg = f"Unavailable: {', '.join(unavailable)}"
            notifier.send(msg, notifier.AlertLevel.URGENT, title="Model Alert")
        except Exception:
            pass

    _log("=== CHECK COMPLETE ===")
    return state


if __name__ == "__main__":
    check()
