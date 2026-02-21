#!/bin/bash
# Install async_analysis_queue cron job

CRON_LINE="*/5 * * * * cd /data/.openclaw/workspace/trading && python3 scripts/async_analysis_queue.py >> logs/cron_async_queue.log 2>&1"

# Check if already installed
if crontab -l 2>/dev/null | grep -q "async_analysis_queue.py"; then
    echo "✅ Cron job already installed"
    exit 0
fi

# Install
(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
echo "✅ Installed async_analysis_queue cron job (runs every 5 minutes)"
