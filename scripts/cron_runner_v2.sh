#!/bin/bash
SCRIPT_NAME="$1"
SCRIPT_PATH="/data/.openclaw/workspace/trading/scripts/${SCRIPT_NAME}.py"
STATE_DIR="/data/.openclaw/workspace/trading/state"
CRON_HEALTH="${STATE_DIR}/cron_health.json"

export SANAD_HOME="/data/.openclaw/workspace/trading"

if [ -z "$SCRIPT_NAME" ]; then
    echo "Usage: cron_runner.sh <script_name>"
    exit 1
fi

# Deploy sync: validate current commit before running any script
bash /data/.openclaw/workspace/trading/scripts/deploy_sync.sh 2>/dev/null || true

python3 "$SCRIPT_PATH" 2>&1

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")

python3 -c "
import json
try:
    with open('${CRON_HEALTH}', 'r') as f:
        data = json.load(f)
except:
    data = {}
data['${SCRIPT_NAME}'] = {'last_run': '${TIMESTAMP}', 'status': 'ok'}
with open('${CRON_HEALTH}', 'w') as f:
    json.dump(data, f, indent=2)
"
