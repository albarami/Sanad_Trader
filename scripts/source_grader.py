#!/usr/bin/env python3
"""
Source Grader — Sprint 5.3.6 + 5.3.7

Deterministic Python. No LLMs.

5.3.6 — UCB1 → Sanad Trust Score integration
Replaces static A-F grades with dynamic UCB1 scores in pipeline.

5.3.7 — Static grade fallback
If UCB1 data corrupted or unavailable, falls back to manual grades.

Used by: sanad_pipeline.py (Stage 2: source trust scoring)
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
UCB1_STATE = BASE_DIR / "state" / "ucb1_scores.json"
STATIC_GRADES_PATH = BASE_DIR / "config" / "source_grades_static.json"
SOURCE_ACCURACY_DIR = BASE_DIR / "genius-memory" / "source-accuracy"

# Static fallback grades (Sanad hadith classification)
STATIC_GRADES = {
    # Signal sources
    "coingecko": {"grade": "B", "score": 65, "label": "Saduq"},
    "dexscreener": {"grade": "B", "score": 60, "label": "Saduq"},
    "birdeye": {"grade": "C", "score": 50, "label": "Maqbul"},
    "pumpfun": {"grade": "C", "score": 45, "label": "Maqbul"},
    "perplexity": {"grade": "B", "score": 60, "label": "Saduq"},
    "binance_listings": {"grade": "A", "score": 80, "label": "Thiqah"},
    "telegram_alpha": {"grade": "D", "score": 30, "label": "Da'if"},
    "onchain_analytics": {"grade": "C", "score": 50, "label": "Maqbul"},
    "whale_alert": {"grade": "C", "score": 45, "label": "Maqbul"},
    "sentiment_scanner": {"grade": "C", "score": 50, "label": "Maqbul"},
    "meme_radar": {"grade": "C", "score": 50, "label": "Maqbul"},
    # Can add more as sources are added
}

GRADE_MAP = {
    "A": {"label": "Thiqah", "min_score": 80},
    "B": {"label": "Saduq", "min_score": 60},
    "C": {"label": "Maqbul", "min_score": 40},
    "D": {"label": "Da'if", "min_score": 20},
    "F": {"label": "Matruk", "min_score": 0},
}


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[GRADER] {ts} {msg}", flush=True)


def _load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def _save_json(path, data):
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def score_to_grade(score: float) -> tuple:
    """Convert numeric score to letter grade + label."""
    if score >= 80:
        return "A", "Thiqah"
    elif score >= 60:
        return "B", "Saduq"
    elif score >= 40:
        return "C", "Maqbul"
    elif score >= 20:
        return "D", "Da'if"
    else:
        return "F", "Matruk"


# ─────────────────────────────────────────────────────────
# 5.3.6 — UCB1 Dynamic Grading
# ─────────────────────────────────────────────────────────

def _get_ucb1_score(source: str) -> dict | None:
    """Get UCB1 score for a source. Returns None if unavailable."""
    try:
        ucb1_data = _load_json(UCB1_STATE)
        if not ucb1_data or not isinstance(ucb1_data, dict):
            return None

        # Check multiple possible structures
        sources = ucb1_data.get("sources", ucb1_data)
        for key in [source, source.lower(), source.replace("_", "-")]:
            if key in sources:
                entry = sources[key]
                if isinstance(entry, dict):
                    score = entry.get("ucb1_score", entry.get("score"))
                    signals = entry.get("total_signals", entry.get("signals", 0))
                    if score is not None and signals >= 5:
                        grade, label = score_to_grade(score)
                        return {
                            "source": source,
                            "score": round(score, 1),
                            "grade": grade,
                            "label": label,
                            "method": "ucb1",
                            "signals": signals,
                            "win_rate": entry.get("win_rate", 0),
                        }
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# 5.3.7 — Static Fallback
# ─────────────────────────────────────────────────────────

def _get_static_grade(source: str) -> dict:
    """Get static fallback grade for a source."""
    # Try custom static grades file first
    custom = _load_json(STATIC_GRADES_PATH)
    if custom and source in custom:
        entry = custom[source]
        return {
            "source": source,
            "score": entry.get("score", 50),
            "grade": entry.get("grade", "C"),
            "label": entry.get("label", "Maqbul"),
            "method": "static_custom",
        }

    # Fall back to hardcoded defaults
    if source.lower() in STATIC_GRADES:
        entry = STATIC_GRADES[source.lower()]
        return {
            "source": source,
            "score": entry["score"],
            "grade": entry["grade"],
            "label": entry["label"],
            "method": "static_default",
        }

    # Unknown source — conservative grade
    return {
        "source": source,
        "score": 35,
        "grade": "D",
        "label": "Da'if",
        "method": "unknown_default",
    }


# ─────────────────────────────────────────────────────────
# Main API: get_source_grade()
# ─────────────────────────────────────────────────────────

def get_source_grade(source: str) -> dict:
    """Get the best available grade for a signal source.

    Priority:
    1. UCB1 dynamic score (if available + enough data)
    2. Static fallback (custom or default)

    Returns: {source, score, grade, label, method}
    """
    # Try UCB1 first (5.3.6)
    ucb1 = _get_ucb1_score(source)
    if ucb1:
        return ucb1

    # Fall back to static (5.3.7)
    return _get_static_grade(source)


def get_all_grades() -> dict:
    """Get grades for all known sources."""
    all_sources = set(STATIC_GRADES.keys())

    # Add UCB1 sources
    ucb1_data = _load_json(UCB1_STATE)
    if ucb1_data:
        sources = ucb1_data.get("sources", ucb1_data)
        if isinstance(sources, dict):
            all_sources.update(sources.keys())

    result = {}
    for source in sorted(all_sources):
        result[source] = get_source_grade(source)
    return result


# ─────────────────────────────────────────────────────────
# Ensure static grades config exists
# ─────────────────────────────────────────────────────────

def ensure_static_config():
    """Create static grades config if missing."""
    if not STATIC_GRADES_PATH.exists():
        STATIC_GRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _save_json(STATIC_GRADES_PATH, STATIC_GRADES)
        _log(f"Created static grades at {STATIC_GRADES_PATH}")


# ─────────────────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    _log("=== SOURCE GRADER TEST ===")
    ensure_static_config()

    print("\n  All source grades:")
    grades = get_all_grades()
    for source, info in grades.items():
        print(f"    {source:25s} → {info['grade']} ({info['score']:5.1f}) {info['label']:10s} [{info['method']}]")

    # Test unknown source
    print(f"\n  Unknown source test:")
    unknown = get_source_grade("random_telegram_group")
    print(f"    random_telegram_group → {unknown['grade']} ({unknown['score']}) [{unknown['method']}]")

    _log("=== TEST COMPLETE ===")
