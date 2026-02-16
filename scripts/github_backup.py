#!/usr/bin/env python3
"""
GitHub State Backup — Sprint 6.1.18
Runs every 6 hours. Pushes state files to GitHub.
Deterministic Python.

Backs up: state/, config/, genius-memory/ (excluding large binary files)
"""

import subprocess
import os
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
REPO_DIR = BASE_DIR / "repo"
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB max per file


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[BACKUP] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _run(cmd, cwd=None):
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=60)
        return r.returncode == 0, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return False, "Command timed out"


def backup():
    _log("=== GITHUB STATE BACKUP ===")

    if not REPO_DIR.exists():
        _log(f"Repo not found at {REPO_DIR}")
        return False

    # Sync state files
    dirs_to_sync = ["state", "config", "genius-memory", "reports"]
    files_copied = 0

    for dirname in dirs_to_sync:
        src = BASE_DIR / dirname
        dst = REPO_DIR / dirname
        if not src.exists():
            continue

        dst.mkdir(parents=True, exist_ok=True)

        for f in src.rglob("*"):
            if f.is_file() and f.stat().st_size < MAX_FILE_SIZE:
                # Skip binary/temp files
                if f.suffix in (".tmp", ".lock", ".session", ".db"):
                    continue
                if "chromadb" in str(f):
                    continue
                rel = f.relative_to(src)
                target = dst / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    import shutil
                    shutil.copy2(f, target)
                    files_copied += 1
                except Exception:
                    pass

    _log(f"Copied {files_copied} files to repo")

    # Git add, commit, push
    ok, out = _run("git add -A", cwd=REPO_DIR)
    if not ok:
        _log(f"git add failed: {out[:200]}")
        return False

    # Check if there are changes
    ok, out = _run("git diff --cached --quiet", cwd=REPO_DIR)
    if ok:
        _log("No changes to commit")
        return True

    # Run secret scanner
    ok, out = _run("python3 scripts/secret_scanner.py", cwd=REPO_DIR)
    if not ok and "BLOCKED" in out:
        _log(f"Secret scanner BLOCKED commit: {out[:200]}")
        return False

    ts = _now().strftime("%Y-%m-%d %H:%M UTC")
    ok, out = _run(f'git commit -m "State backup {ts}"', cwd=REPO_DIR)
    if not ok:
        _log(f"git commit failed: {out[:200]}")
        return False

    ok, out = _run("git push origin main", cwd=REPO_DIR)
    if ok:
        _log(f"Pushed to GitHub successfully")
        return True
    else:
        _log(f"git push failed: {out[:200]}")
        return False


if __name__ == "__main__":
    backup()
