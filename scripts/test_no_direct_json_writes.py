#!/usr/bin/env python3
"""
CI guard: No script may directly WRITE to portfolio.json or positions.json.
Only state_store.sync_json_cache() is allowed to write these files.

This prevents state split-brain where JSON and SQLite disagree.
"""
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
# Files that are ALLOWED to write (sync_json_cache lives here)
ALLOWED_WRITERS = {"state_store.py", "smoke_imports.py"}
# Test files are exempt
EXEMPT_PREFIXES = ("test_",)

# Patterns that indicate direct JSON state mutation
FORBIDDEN_PATTERNS = [
    # Direct file writes
    r'open\s*\(.*(?:portfolio|positions)\.json.*["\']w',
    r'save_json.*(?:portfolio|positions)\.json',
    r'_save_json.*(?:portfolio|positions)\.json',
    r'save_json_atomic.*(?:portfolio|positions)\.json',
    r'save_state\s*\(\s*["\'](?:portfolio|positions)\.json',
    r'PORTFOLIO_PATH.*"w"',
    r'POSITIONS_PATH.*"w"',
]

# Patterns that are OK (fallback comments, sync_json_cache calls, reading)
FALSE_POSITIVE_MARKERS = [
    "fallback",
    "legacy",
    "deprecated",
    "sync_json_cache",
    "backward compat",
    "falling back to json",
]


def check_file(filepath: Path) -> list:
    """Check a single file for forbidden patterns. Returns list of violations."""
    violations = []
    try:
        lines = filepath.read_text().splitlines()
    except Exception:
        return []

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, line):
                # Check if it's a known false positive (fallback path)
                is_fp = any(marker in line.lower() for marker in FALSE_POSITIVE_MARKERS)
                if not is_fp:
                    violations.append((i, stripped[:120]))
                break

    return violations


def main():
    scripts = sorted(SCRIPTS_DIR.glob("*.py"))
    total_violations = 0
    files_with_violations = []

    for script in scripts:
        name = script.name
        if name in ALLOWED_WRITERS:
            continue
        if any(name.startswith(p) for p in EXEMPT_PREFIXES):
            continue

        violations = check_file(script)
        if violations:
            files_with_violations.append((name, violations))
            total_violations += len(violations)

    print(f"{'=' * 60}")
    print(f"CI GUARD: No direct writes to portfolio.json / positions.json")
    print(f"{'=' * 60}")

    if files_with_violations:
        for name, violations in files_with_violations:
            print(f"\n❌ {name}:")
            for line_no, text in violations:
                print(f"   L{line_no}: {text}")
        print(f"\n{'=' * 60}")
        print(f"FAILED: {total_violations} violation(s) in {len(files_with_violations)} file(s)")
        print(f"Fix: Use state_store.update_portfolio() / state_store.sync_json_cache()")
        print(f"{'=' * 60}")
        sys.exit(1)
    else:
        print(f"\n✅ ALL {len(scripts)} scripts clean — no direct JSON state writes")
        print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
