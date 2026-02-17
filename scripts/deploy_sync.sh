#!/usr/bin/env bash
# deploy_sync.sh — Sync repo scripts to runtime, gated by smoke test.
# Safe to run from cron or manually after commits.
set -euo pipefail

REPO="/data/.openclaw/workspace/trading"
RUNTIME="/data/.openclaw/workspace/trading"
SCRIPTS_SRC="$REPO/scripts"
SCRIPTS_DST="$RUNTIME/scripts"
DEPLOYED_FILE="$RUNTIME/state/deployed_commit.txt"

# Since repo root IS runtime root now, this script validates
# that the current HEAD passes smoke test and records the commit.
# If repo and runtime ever split again, uncomment the rsync below.

HEAD="$(git -C "$REPO" rev-parse HEAD)"
DEPLOYED="$(cat "$DEPLOYED_FILE" 2>/dev/null || true)"

if [[ "$HEAD" == "$DEPLOYED" ]]; then
    echo "deploy_sync: already at $HEAD"
    exit 0
fi

echo "deploy_sync: validating $HEAD"

# If trees are separate (future-proof), uncomment:
# rsync -a --delete --exclude '__pycache__/' --exclude '*.pyc' "$SCRIPTS_SRC/" "$SCRIPTS_DST/"

# Gate: smoke test must pass
cd "$SCRIPTS_DST"
if python3 smoke_imports.py; then
    mkdir -p "$(dirname "$DEPLOYED_FILE")"
    echo "$HEAD" > "$DEPLOYED_FILE"
    echo "deploy_sync: deployed $HEAD OK"
else
    echo "deploy_sync: SMOKE TEST FAILED — deployment blocked"
    exit 1
fi
