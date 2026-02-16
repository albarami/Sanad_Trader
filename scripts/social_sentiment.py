#!/usr/bin/env python3
"""
Social Sentiment Scanner — Sprint 6.1.12 + 6.1.20
Runs every 15 min. Scans social sources for sentiment on tracked tokens.
Also covers Twitter/X mention tracking (6.1.20).

Sources (by availability):
1. LunarCrush (free tier) — social volume + sentiment
2. Telegram sniffer signals — alpha group sentiment
3. CoinGecko community data — reddit/telegram stats
4. Twitter/X API (when key available)
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
SIGNALS_DIR = BASE_DIR / "signals" / "sentiment"
POSITIONS_PATH = STATE_DIR / "positions.json"
WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.json"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[SENTIMENT] {ts} {msg}", flush=True)


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


def _get_tracked_tokens() -> list:
    """Get tokens to monitor: open positions + watchlist."""
    tokens = set()

    # Open positions
    positions = _load_json(POSITIONS_PATH, {})
    if isinstance(positions, list):
        for p in positions:
            if p.get("status") == "open":
                tokens.add(p.get("token", p.get("symbol", "")).replace("USDT", ""))
    elif isinstance(positions, dict):
        for k, v in positions.items():
            if isinstance(v, dict) and v.get("status") == "open":
                tokens.add(k.replace("USDT", ""))

    # Watchlist
    watchlist = _load_json(WATCHLIST_PATH, {"tokens": ["BTC", "ETH", "SOL", "PEPE", "WIF", "BONK"]})
    tokens.update(watchlist.get("tokens", []))

    return list(tokens)


def scan_coingecko_sentiment(token: str) -> dict | None:
    """Get community sentiment data from CoinGecko."""
    try:
        import env_loader
        api_key = env_loader.get_key("COINGECKO_API_KEY")

        # Map common symbols to CoinGecko IDs
        cg_ids = {
            "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
            "PEPE": "pepe", "WIF": "dogwifcoin", "BONK": "bonk",
            "DOGE": "dogecoin", "SHIB": "shiba-inu", "FLOKI": "floki",
        }
        cg_id = cg_ids.get(token.upper())
        if not cg_id:
            return None

        headers = {}
        if api_key:
            headers["x-cg-demo-key"] = api_key

        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{cg_id}",
            headers=headers,
            params={"localization": "false", "tickers": "false",
                    "market_data": "false", "community_data": "true",
                    "developer_data": "false"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            community = data.get("community_data", {})
            sentiment = data.get("sentiment_votes_up_percentage", 50)
            return {
                "source": "coingecko",
                "token": token,
                "sentiment_score": sentiment,
                "reddit_subscribers": community.get("reddit_subscribers", 0),
                "reddit_active_48h": community.get("reddit_accounts_active_48h", 0),
                "telegram_members": community.get("telegram_channel_user_count", 0),
            }
        return None
    except Exception as e:
        _log(f"CoinGecko sentiment error for {token}: {e}")
        return None


def scan_telegram_signals() -> list:
    """Check recent Telegram sniffer signals for sentiment."""
    tg_state = _load_json(STATE_DIR / "telegram_sniffer_state.json", {})
    recent = tg_state.get("recent_signals", [])
    cutoff = (_now() - timedelta(hours=1)).isoformat()
    return [s for s in recent if s.get("timestamp", "") >= cutoff]


def scan_twitter(token: str) -> dict | None:
    """Scan Twitter/X for mentions (when API available)."""
    try:
        import env_loader
        bearer = env_loader.get_key("TWITTER_BEARER_TOKEN")
        if not bearer:
            return None

        resp = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers={"Authorization": f"Bearer {bearer}"},
            params={
                "query": f"${token} OR #{token} -is:retweet",
                "max_results": 10,
                "tweet.fields": "public_metrics,created_at",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            tweets = data.get("data", [])
            total_likes = sum(t.get("public_metrics", {}).get("like_count", 0) for t in tweets)
            total_retweets = sum(t.get("public_metrics", {}).get("retweet_count", 0) for t in tweets)
            return {
                "source": "twitter",
                "token": token,
                "mentions_1h": len(tweets),
                "total_likes": total_likes,
                "total_retweets": total_retweets,
                "engagement_score": min(100, (total_likes + total_retweets * 3) // max(len(tweets), 1)),
            }
        return None
    except Exception as e:
        _log(f"Twitter scan error for {token}: {e}")
        return None


def run():
    _log("=== SOCIAL SENTIMENT SCAN ===")

    tokens = _get_tracked_tokens()
    if not tokens:
        tokens = ["BTC", "ETH", "SOL"]  # Default watchlist

    _log(f"Scanning {len(tokens)} tokens: {', '.join(tokens[:10])}")

    all_sentiment = {}
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    for token in tokens:
        sentiment = {"token": token, "sources": [], "timestamp": _now().isoformat()}

        # CoinGecko
        cg = scan_coingecko_sentiment(token)
        if cg:
            sentiment["sources"].append(cg)

        # Twitter
        tw = scan_twitter(token)
        if tw:
            sentiment["sources"].append(tw)

        # Aggregate score (0-100)
        scores = [s.get("sentiment_score", s.get("engagement_score", 50))
                  for s in sentiment["sources"]]
        sentiment["aggregate_score"] = round(sum(scores) / len(scores), 1) if scores else 50
        sentiment["source_count"] = len(sentiment["sources"])

        all_sentiment[token] = sentiment

    # Add Telegram signals
    tg_signals = scan_telegram_signals()
    if tg_signals:
        _log(f"  Telegram signals: {len(tg_signals)} in last hour")

    # Save
    state = {
        "last_scan": _now().isoformat(),
        "tokens_scanned": len(tokens),
        "sentiment": all_sentiment,
        "telegram_signals": len(tg_signals),
    }
    _save_json(STATE_DIR / "social_sentiment_state.json", state)

    # Save individual signals
    for token, data in all_sentiment.items():
        if data["source_count"] > 0:
            fname = f"{_now().strftime('%Y%m%d_%H%M')}_{token}_sentiment.json"
            _save_json(SIGNALS_DIR / fname, data)

    _log(f"Scanned {len(tokens)} tokens, {sum(1 for s in all_sentiment.values() if s['source_count'] > 0)} with data")
    _log("=== SCAN COMPLETE ===")
    return state


if __name__ == "__main__":
    run()
