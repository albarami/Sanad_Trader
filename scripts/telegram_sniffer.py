#!/usr/bin/env python3
"""
Telegram Alpha Group Sniffer — Sprint 1.2.16 + 3.8.10

Monitors Telegram alpha groups for token calls & contract addresses.
Uses Telethon (user account, NOT bot API).

Emits signals to: signals/telegram/
Reads config from: config/telegram_groups.json

SAFETY:
- Randomized 200-800ms jitter delay (mimic human reading)
- Never sends messages to groups (read-only)
- Rate limited: max 1 signal per token per hour
"""

import asyncio
import json
import os
import re
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
SIGNALS_DIR = BASE_DIR / "signals" / "telegram"
CONFIG_PATH = BASE_DIR / "config" / "telegram_groups.json"
STATE_PATH = STATE_DIR / "telegram_sniffer_state.json"

sys.path.insert(0, str(SCRIPT_DIR))


# ─────────────────────────────────────────────────────────
# Config & helpers
# ─────────────────────────────────────────────────────────

def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[TG-SNIFF] {ts} {msg}", flush=True)


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


# ─────────────────────────────────────────────────────────
# Token/Contract Detection Patterns
# ─────────────────────────────────────────────────────────

# Solana contract address (base58, 32-44 chars)
SOL_CONTRACT_RE = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')

# Ethereum contract address (0x + 40 hex)
ETH_CONTRACT_RE = re.compile(r'\b0x[a-fA-F0-9]{40}\b')

# Ticker symbols like $BTC, $PEPE, $WIF
TICKER_RE = re.compile(r'\$([A-Z]{2,10})\b')

# Common call keywords
CALL_KEYWORDS = [
    "buy", "buying", "long", "entry", "accumulate", "ape", "gem", "alpha",
    "moon", "pump", "100x", "1000x", "dyor", "nfa", "not financial advice",
    "just bought", "loading", "filled", "bags", "CA:", "contract:", "mint:",
    "token:",
]

# Signal strength keywords (higher confidence)
STRONG_KEYWORDS = [
    "CA:", "contract:", "mint:", "just bought", "filled my bags",
    "aping in", "entry zone", "buy zone",
]


def detect_tokens(text: str) -> list:
    """Detect contract addresses and tickers from message text."""
    findings = []

    # Solana contracts
    for match in SOL_CONTRACT_RE.finditer(text):
        addr = match.group()
        # Filter out common false positives (too short or common words)
        if len(addr) >= 32 and not addr.isalpha():
            findings.append({
                "type": "solana_contract",
                "value": addr,
                "chain": "solana",
            })

    # Ethereum contracts
    for match in ETH_CONTRACT_RE.finditer(text):
        findings.append({
            "type": "eth_contract",
            "value": match.group(),
            "chain": "ethereum",
        })

    # Tickers
    for match in TICKER_RE.finditer(text):
        ticker = match.group(1)
        # Skip common false positives
        if ticker not in {"USD", "THE", "FOR", "AND", "NOT", "BUT", "ALL", "ARE"}:
            findings.append({
                "type": "ticker",
                "value": ticker,
                "chain": "unknown",
            })

    return findings


def calc_signal_strength(text: str, findings: list) -> float:
    """Calculate signal strength 0-100 based on message content."""
    text_lower = text.lower()
    score = 0

    # Base: contract address found
    has_contract = any(f["type"] in ("solana_contract", "eth_contract") for f in findings)
    if has_contract:
        score += 40

    # Call keywords
    keyword_hits = sum(1 for kw in CALL_KEYWORDS if kw.lower() in text_lower)
    score += min(keyword_hits * 5, 25)

    # Strong keywords
    strong_hits = sum(1 for kw in STRONG_KEYWORDS if kw.lower() in text_lower)
    score += min(strong_hits * 10, 20)

    # Ticker found
    has_ticker = any(f["type"] == "ticker" for f in findings)
    if has_ticker:
        score += 10

    # Negative signals (reduce confidence)
    if "scam" in text_lower or "rug" in text_lower or "fake" in text_lower:
        score -= 30
    if "sell" in text_lower or "dump" in text_lower or "exit" in text_lower:
        score -= 15

    return max(0, min(100, score))


# ─────────────────────────────────────────────────────────
# Signal Emission
# ─────────────────────────────────────────────────────────

def _check_cooldown(token_key: str, state: dict, cooldown_hours: int = 1) -> bool:
    """Check if token is in cooldown."""
    cooldowns = state.get("cooldowns", {})
    if token_key in cooldowns:
        try:
            until = datetime.fromisoformat(cooldowns[token_key])
            if _now() < until:
                return True
        except (ValueError, TypeError):
            pass
    return False


def _set_cooldown(token_key: str, state: dict, cooldown_hours: int = 1):
    """Set cooldown for a token."""
    if "cooldowns" not in state:
        state["cooldowns"] = {}
    state["cooldowns"][token_key] = (_now() + timedelta(hours=cooldown_hours)).isoformat()


def emit_signal(finding: dict, strength: float, message_text: str,
                group_name: str, group_grade: str, state: dict) -> dict | None:
    """Emit a signal if it passes filters."""
    token_key = finding["value"][:20]

    # Cooldown check
    if _check_cooldown(token_key, state):
        _log(f"  Cooldown: {token_key} — skipping")
        return None

    # Minimum strength
    if strength < 30:
        _log(f"  Weak signal ({strength:.0f}/100) for {token_key} — skipping")
        return None

    now = _now()
    signal = {
        "source": "telegram_sniffer",
        "source_group": group_name,
        "source_grade": group_grade,
        "token": token_key,
        "token_type": finding["type"],
        "chain": finding["chain"],
        "signal_strength": strength,
        "message_preview": message_text[:200],
        "timestamp": now.isoformat(),
        "type": "ALPHA_CALL",
    }

    # Save signal
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{now.strftime('%Y%m%d_%H%M%S')}_{token_key[:12]}.json"
    _save_json(SIGNALS_DIR / fname, signal)

    # Set cooldown
    _set_cooldown(token_key, state)

    _log(f"  SIGNAL [{strength:.0f}/100]: {finding['type']} {token_key} from {group_name} ({group_grade})")
    return signal


# ─────────────────────────────────────────────────────────
# Default config
# ─────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "groups": [
        {
            "name": "Example Alpha Group",
            "id": -1001234567890,
            "grade": "C",
            "enabled": False,
            "notes": "Replace with real group IDs"
        }
    ],
    "settings": {
        "min_signal_strength": 30,
        "cooldown_hours": 1,
        "jitter_min_ms": 200,
        "jitter_max_ms": 800,
        "max_signals_per_hour": 10,
    }
}


def ensure_config():
    """Create default config if missing."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _save_json(CONFIG_PATH, DEFAULT_CONFIG)
        _log(f"Created default config at {CONFIG_PATH}")
        _log("Edit config/telegram_groups.json to add real group IDs")


# ─────────────────────────────────────────────────────────
# Main: Telethon listener
# ─────────────────────────────────────────────────────────

async def run_listener():
    """Main listener loop using Telethon."""
    try:
        from telethon import TelegramClient, events
    except ImportError:
        _log("ERROR: Telethon not installed. Run: pip install telethon")
        _log("Then re-run this script.")
        return

    # Load credentials from environment
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        # Try .env file
        env_path = BASE_DIR / "config" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("TELEGRAM_API_ID="):
                    api_id = line.split("=", 1)[1].strip()
                elif line.startswith("TELEGRAM_API_HASH="):
                    api_hash = line.split("=", 1)[1].strip()

    if not api_id or not api_hash:
        _log("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH not set")
        return

    # Load config
    ensure_config()
    config = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
    groups = [g for g in config.get("groups", []) if g.get("enabled")]
    settings = config.get("settings", DEFAULT_CONFIG["settings"])

    if not groups:
        _log("No enabled groups in config. Edit config/telegram_groups.json")
        _log("Add group IDs and set enabled: true")
        return

    group_ids = [g["id"] for g in groups]
    group_map = {g["id"]: g for g in groups}

    _log(f"Monitoring {len(groups)} groups: {[g['name'] for g in groups]}")

    # State
    state = _load_json(STATE_PATH, {"signals_emitted": 0, "messages_scanned": 0, "cooldowns": {}})

    session_path = str(BASE_DIR / "state" / "telegram_session")

    async with TelegramClient(session_path, int(api_id), api_hash) as client:
        _log("Connected to Telegram")

        @client.on(events.NewMessage(chats=group_ids))
        async def handler(event):
            # Anti-detection jitter
            jitter = random.randint(
                settings.get("jitter_min_ms", 200),
                settings.get("jitter_max_ms", 800)
            ) / 1000
            await asyncio.sleep(jitter)

            text = event.raw_text or ""
            if not text or len(text) < 5:
                return

            state["messages_scanned"] = state.get("messages_scanned", 0) + 1

            # Detect tokens/contracts
            findings = detect_tokens(text)
            if not findings:
                return

            # Get group info
            chat_id = event.chat_id
            group_info = group_map.get(chat_id, {"name": str(chat_id), "grade": "D"})
            group_name = group_info.get("name", str(chat_id))
            group_grade = group_info.get("grade", "D")

            # Calculate strength
            strength = calc_signal_strength(text, findings)

            # Emit signals for each finding
            for finding in findings:
                signal = emit_signal(finding, strength, text, group_name, group_grade, state)
                if signal:
                    state["signals_emitted"] = state.get("signals_emitted", 0) + 1

            # Save state periodically
            state["last_scan"] = _now().isoformat()
            _save_json(STATE_PATH, state)

        _log("Listening for messages...")
        await client.run_until_disconnected()


# ─────────────────────────────────────────────────────────
# Test mode (no Telethon required)
# ─────────────────────────────────────────────────────────

def test_detection():
    """Test token detection without Telegram connection."""
    _log("=== DETECTION TEST (offline) ===")

    test_messages = [
        "New gem alert! $PEPE looking bullish, aping in now 🚀",
        "CA: 7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr just bought 10 SOL worth",
        "Check out 0x6982508145454Ce325dDbE47a25d4ec3d2311933 on ETH, loading my bags",
        "The weather is nice today, going for a walk",
        "$WIF entry zone 0.85-0.90, NFA DYOR",
        "SCAM ALERT: Do not buy this token, it's a rug pull!",
    ]

    state = {"cooldowns": {}}

    for i, msg in enumerate(test_messages):
        print(f"\n  Message {i+1}: {msg[:60]}...")
        findings = detect_tokens(msg)
        strength = calc_signal_strength(msg, findings)
        print(f"    Findings: {len(findings)}, Strength: {strength:.0f}/100")

        for f in findings:
            print(f"    → {f['type']}: {f['value'][:30]}... ({f['chain']})")
            signal = emit_signal(f, strength, msg, "test-group", "B", state)
            if signal:
                print(f"    → SIGNAL EMITTED")

    ensure_config()
    _log(f"Config template at: {CONFIG_PATH}")
    _log("=== TEST COMPLETE ===")


async def run_snapshot(max_messages: int = 20, max_age_min: int = 15):
    """Snapshot mode: connect, read recent messages from channels, parse, disconnect.
    Designed for cron execution (no persistent connection needed).
    """
    try:
        from telethon import TelegramClient
    except ImportError:
        _log("ERROR: Telethon not installed")
        return

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        env_path = BASE_DIR / "config" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("TELEGRAM_API_ID="):
                    api_id = line.split("=", 1)[1].strip()
                elif line.startswith("TELEGRAM_API_HASH="):
                    api_hash = line.split("=", 1)[1].strip()

    if not api_id or not api_hash:
        _log("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH not set")
        return

    session_path = str(BASE_DIR / "state" / "telegram_session")
    if not Path(session_path + ".session").exists():
        _log("ERROR: No Telegram session found. Run: python3 scripts/telegram_sniffer_auth.py")
        return

    ensure_config()
    config = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
    groups = [g for g in config.get("groups", []) if g.get("enabled")]
    settings = config.get("settings", DEFAULT_CONFIG["settings"])

    if not groups:
        _log("No enabled groups in config")
        return

    state = _load_json(STATE_PATH, {"signals_emitted": 0, "messages_scanned": 0, "cooldowns": {}})
    cutoff = _now() - timedelta(minutes=max_age_min)
    signals_found = 0

    _log(f"Snapshot mode: reading last {max_messages} messages from {len(groups)} channels (max {max_age_min}min old)")

    async with TelegramClient(session_path, int(api_id), api_hash) as client:
        for group in groups:
            group_id = group["id"]
            group_name = group.get("name", group_id)
            group_grade = group.get("grade", "C")

            try:
                entity = await client.get_entity(group_id)
                messages = await client.get_messages(entity, limit=max_messages)

                for msg in messages:
                    if not msg.text or len(msg.text) < 5:
                        continue
                    if msg.date.replace(tzinfo=timezone.utc) < cutoff:
                        continue

                    state["messages_scanned"] = state.get("messages_scanned", 0) + 1

                    findings = detect_tokens(msg.text)
                    if not findings:
                        continue

                    strength = calc_signal_strength(msg.text, findings)
                    if strength < settings.get("min_signal_strength", 30):
                        continue

                    # Check hourly limit
                    if state.get("signals_this_hour", 0) >= settings.get("max_signals_per_hour", 10):
                        break

                    for finding in findings:
                        token_key = finding.get("token", finding.get("contract", "?"))
                        if _check_cooldown(token_key, state, settings.get("cooldown_hours", 1)):
                            continue

                        emit_signal(finding, strength, msg.text, group_name, group_grade, group.get("strategy", ""))
                        _set_cooldown(token_key, state, settings.get("cooldown_hours", 1))
                        state["signals_emitted"] = state.get("signals_emitted", 0) + 1
                        state["signals_this_hour"] = state.get("signals_this_hour", 0) + 1
                        signals_found += 1

                _log(f"  {group_name}: scanned {len(messages)} messages")
            except Exception as e:
                _log(f"  {group_name}: error — {e}")

    _save_json(STATE_PATH, state)
    _log(f"Snapshot done: {signals_found} signals from {len(groups)} channels")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run offline detection test")
    parser.add_argument("--listen", action="store_true", help="Start live listener")
    parser.add_argument("--snapshot", action="store_true", help="Snapshot: read recent messages and exit")
    args = parser.parse_args()

    if args.listen:
        asyncio.run(run_listener())
    elif args.snapshot:
        asyncio.run(run_snapshot())
    else:
        test_detection()
