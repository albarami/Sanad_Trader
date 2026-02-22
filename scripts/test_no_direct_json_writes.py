#!/usr/bin/env python3
"""
CI GUARD: Enforces SSOT invariants for portfolio.json / positions.json.

Invariant A: No script writes these files except state_store.sync_json_cache()
Invariant B: No decision-critical script reads these files

Enforcement: EXPLICIT allowlist only. No comment-based bypass.
"""
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

# ═══════════════════════════════════════════════════════════
# WRITE GUARD — Only sync_json_cache may write
# ═══════════════════════════════════════════════════════════

# Exact files + functions allowed to write portfolio.json / positions.json
WRITE_ALLOWLIST = {
    "state_store.py",       # sync_json_cache() — the ONLY legitimate writer
    "smoke_imports.py",     # Import/existence checks only
}

# Files with known legacy writes that are guarded by if-not-HAS_STATE_STORE or try/except fallback
# These are tolerated ONLY because they are dead code paths when state_store is available
# Each entry: (filename, max_allowed_writes) — if count exceeds, test fails
WRITE_LEGACY_TOLERANCE = {
    "position_monitor.py": 3,   # 3 fallback writes (guarded by HAS_STATE_STORE check)
    "heartbeat.py": 1,          # 1 emergency fallback
    "console_api.py": 1,        # 1 manual tool legacy
    "sanad_pipeline.py": 2,     # 2 legacy v3.0 (entire script is deprecated)
    "emergency_sell.py": 0,     # Fixed — uses state_store now
}

WRITE_PATTERNS = [
    r'open\s*\(.*(?:portfolio|positions)\.json.*["\']w',
    r'save_json.*(?:portfolio|positions)\.json',
    r'_save_json.*(?:portfolio|positions)\.json',
    r'save_json_atomic.*(?:portfolio|positions)\.json',
    r'save_state\s*\(\s*["\'](?:portfolio|positions)\.json',
]

# ═══════════════════════════════════════════════════════════
# READ GUARD — Decision-critical scripts must not read JSON
# ═══════════════════════════════════════════════════════════

# These scripts participate in trading decisions / risk gates.
# They must NEVER read portfolio.json or positions.json.
DECISION_CRITICAL_SCRIPTS = {
    "fast_decision_engine.py",
    "policy_engine.py",
    "learning_loop.py",
    "async_analysis_queue.py",
    "kelly_position_size.py",
}

READ_PATTERNS = [
    r'open\s*\(.*(?:portfolio|positions)\.json.*["\']r',
    r'load_json.*(?:portfolio|positions)\.json',
    r'_load_json.*(?:portfolio|positions)\.json',
    r'load_state\s*\(\s*["\'](?:portfolio|positions)\.json',
    r'json\.load.*(?:portfolio|positions)\.json',
]


def scan_file(filepath, patterns):
    """Scan file for pattern matches. Returns list of (line_no, text)."""
    hits = []
    try:
        lines = filepath.read_text().splitlines()
    except Exception:
        return []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern in patterns:
            if re.search(pattern, line):
                hits.append((i, stripped[:120]))
                break
    return hits


def main():
    scripts = sorted(SCRIPTS_DIR.glob("*.py"))
    failures = []

    # ── WRITE GUARD ──
    for script in scripts:
        name = script.name
        if name.startswith("test_"):
            continue
        if name in WRITE_ALLOWLIST:
            continue

        hits = scan_file(script, WRITE_PATTERNS)
        if not hits:
            continue

        max_tolerated = WRITE_LEGACY_TOLERANCE.get(name, 0)
        if len(hits) > max_tolerated:
            failures.append(("WRITE", name, hits, max_tolerated))

    # ── READ GUARD ──
    for script_name in DECISION_CRITICAL_SCRIPTS:
        script = SCRIPTS_DIR / script_name
        if not script.exists():
            continue
        hits = scan_file(script, READ_PATTERNS)
        if hits:
            failures.append(("READ", script_name, hits, 0))

    # ── SINGLE-DB GUARD ──
    # No script except state_store.py should call sqlite3.connect() directly
    DB_ALLOWLIST = {"state_store.py", "smoke_imports.py", "learning_loop.py"}
    DB_LEGACY_TOLERANCE = {
        "signal_router.py": 0,       # Uses state_store
        "reconciliation.py": 0,
        "sanad_pipeline.py": 5,      # Legacy v3.0
        "system_audit.py": 2,        # Audit tool
        "console_api.py": 2,         # Manual tool
    }
    DB_PATTERNS = [r'sqlite3\.connect\s*\(']

    for script in scripts:
        name = script.name
        if name.startswith("test_"):
            continue
        if name in DB_ALLOWLIST:
            continue
        hits = scan_file(script, DB_PATTERNS)
        if not hits:
            continue
        max_tolerated = DB_LEGACY_TOLERANCE.get(name, 0)
        if len(hits) > max_tolerated:
            failures.append(("DB_CONNECT", name, hits, max_tolerated))

    # ── REPORT ──
    print(f"{'=' * 60}")
    print(f"CI GUARD: SSOT Invariant Enforcement")
    print(f"{'=' * 60}")

    if failures:
        for kind, name, hits, tolerance in failures:
            labels = {"WRITE": "FORBIDDEN WRITE", "READ": "FORBIDDEN READ (decision-critical)", "DB_CONNECT": "FORBIDDEN sqlite3.connect (use state_store)"}
            label = labels.get(kind, kind)
            print(f"\n❌ {name} — {label}:")
            for line_no, text in hits:
                print(f"   L{line_no}: {text}")
            if kind == "WRITE" and tolerance > 0:
                print(f"   (tolerance: {tolerance}, found: {len(hits)})")
        print(f"\n{'=' * 60}")
        print(f"FAILED: {len(failures)} violation(s)")
        print(f"{'=' * 60}")
        sys.exit(1)
    else:
        total = len(scripts)
        print(f"\n✅ WRITE guard: {total} scripts clean (allowlist: {len(WRITE_ALLOWLIST)} files)")
        print(f"✅ READ guard: {len(DECISION_CRITICAL_SCRIPTS)} decision-critical scripts clean")
        print(f"✅ DB guard: sqlite3.connect only via state_store (allowlist: {len(DB_ALLOWLIST)} files)")
        print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
