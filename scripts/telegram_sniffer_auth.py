#!/usr/bin/env python3
"""One-time Telegram auth. Run interactively: python3 scripts/telegram_sniffer_auth.py"""
import os, sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
env_path = BASE_DIR / "config" / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from telethon.sync import TelegramClient

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
session_path = str(BASE_DIR / "state" / "telegram_session")

print("Authenticating with Telegram...")
print("You'll be asked for your phone number and a code sent to Telegram.")
with TelegramClient(session_path, api_id, api_hash) as client:
    me = client.get_me()
    print(f"Authenticated as: {me.first_name} ({me.phone})")
    print(f"Session saved to: {session_path}.session")
    print("The sniffer can now run non-interactively.")
