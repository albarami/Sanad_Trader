#!/usr/bin/env python3
"""
Batch update secondary scripts to use state_store instead of JSON reads.
Ticket 12: Unified State Layer
"""

import re
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent

# List of secondary scripts to update
SECONDARY_SCRIPTS = [
    "daily_report.py",
    "reconciliation.py",
    "emergency_sell.py",
    "sentiment_exit_trigger.py",
    "whale_exit_trigger.py",
    "social_sentiment.py",
    "sentiment_scanner.py",
    "tradeability_scorer.py",
    "whale_discovery.py",
    "daily_pnl_reset.py",
    "console_api.py",
    "dex_shadow.py",
    "weekly_analysis.py",
    "smoke_imports.py",
]

IMPORT_BLOCK = """
# Import state_store for unified state management (Ticket 12)
import sys
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
try:
    import state_store
    HAS_STATE_STORE = True
except ImportError:
    HAS_STATE_STORE = False
"""

def add_state_store_import(content):
    """Add state_store import if not present."""
    if "import state_store" in content:
        return content
    
    # Find first import statement and add before it
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if line.strip().startswith('import ') or line.strip().startswith('from '):
            # Insert before first import
            lines.insert(i, IMPORT_BLOCK)
            return '\n'.join(lines)
    
    # No imports found, add at top after shebang/docstring
    for i, line in enumerate(lines):
        if i > 0 and not line.strip().startswith('#') and not line.strip().startswith('"""'):
            lines.insert(i, IMPORT_BLOCK)
            return '\n'.join(lines)
    
    return content

def update_script(script_path):
    """Update a script to use state_store."""
    if not script_path.exists():
        print(f"SKIP: {script_path.name} not found")
        return False
    
    content = script_path.read_text()
    original_content = content
    
    # Add import if needed
    content = add_state_store_import(content)
    
    # Replace portfolio.json reads
    # Pattern: load_json(.*portfolio.json.*)
    content = re.sub(
        r'(\w+)\s*=\s*load_json\([^)]*portfolio\.json[^)]*\)',
        r'\1 = state_store.get_portfolio() if HAS_STATE_STORE else load_json(STATE_DIR / "portfolio.json")',
        content
    )
    
    # Pattern: json.load(open(...portfolio.json...))
    content = re.sub(
        r'json\.load\(open\([^)]*portfolio\.json[^)]*\)\)',
        r'state_store.get_portfolio() if HAS_STATE_STORE else json.load(open(STATE_DIR / "portfolio.json"))',
        content
    )
    
    # Replace positions.json reads
    content = re.sub(
        r'(\w+)\s*=\s*load_json\([^)]*positions\.json[^)]*\)',
        r'\1 = {"positions": state_store.get_all_positions()} if HAS_STATE_STORE else load_json(STATE_DIR / "positions.json")',
        content
    )
    
    if content != original_content:
        script_path.write_text(content)
        print(f"✓ Updated: {script_path.name}")
        return True
    else:
        print(f"  No changes: {script_path.name}")
        return False

def main():
    print("Batch updating secondary scripts for Ticket 12...\n")
    
    updated_count = 0
    for script_name in SECONDARY_SCRIPTS:
        script_path = SCRIPTS_DIR / script_name
        if update_script(script_path):
            updated_count += 1
    
    print(f"\nDone: {updated_count}/{len(SECONDARY_SCRIPTS)} scripts updated")

if __name__ == "__main__":
    main()
