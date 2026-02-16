#!/usr/bin/env python3
"""
Security Audit — Sprint 6.1.17
Runs Friday 22:00 QAT (19:00 UTC).
Checks VPS security, file integrity, exposed secrets.
Deterministic Python.
"""

import subprocess
import os
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
AUDIT_PATH = STATE_DIR / "security_audit.json"

import sys
sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[AUDIT] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", -1


def check_env_permissions():
    """Check .env file permissions."""
    issues = []
    env_files = list(BASE_DIR.rglob(".env*"))
    for f in env_files:
        mode = oct(f.stat().st_mode)[-3:]
        if mode not in ("600", "400"):
            issues.append(f"{f}: permissions {mode} (should be 600)")
    return issues


def check_exposed_secrets():
    """Scan for accidentally committed secrets."""
    issues = []
    patterns = ["sk-", "xoxb-", "ghp_", "AKIA", "-----BEGIN PRIVATE KEY"]
    for f in BASE_DIR.rglob("*.py"):
        try:
            content = f.read_text()
            for pattern in patterns:
                if pattern in content:
                    issues.append(f"{f.name}: contains pattern '{pattern}'")
        except Exception:
            pass
    return issues


def check_file_integrity():
    """Hash critical scripts for tamper detection."""
    hashes = {}
    scripts_dir = SCRIPT_DIR
    if scripts_dir.exists():
        for f in sorted(scripts_dir.glob("*.py")):
            try:
                h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
                hashes[f.name] = h
            except Exception:
                hashes[f.name] = "ERROR"
    return hashes


def check_open_ports():
    """Check for unexpected open ports."""
    out, _ = _run("ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null")
    return out


def check_disk_space():
    """Check available disk space."""
    out, _ = _run("df -h / | tail -1")
    return out


def check_process_anomalies():
    """Check for suspicious processes."""
    out, _ = _run("ps aux --sort=-%mem | head -10")
    return out


def check_ssh_auth():
    """Check recent SSH auth attempts."""
    out, _ = _run("tail -20 /var/log/auth.log 2>/dev/null || echo 'no auth log access'")
    failed = out.count("Failed password") if out else 0
    return failed


def run():
    _log("=== SECURITY AUDIT ===")

    results = {
        "timestamp": _now().isoformat(),
        "checks": {},
        "issues": [],
        "severity": "OK",
    }

    # 1. .env permissions
    env_issues = check_env_permissions()
    results["checks"]["env_permissions"] = {"issues": env_issues, "pass": len(env_issues) == 0}
    results["issues"].extend(env_issues)
    _log(f"  .env permissions: {'PASS' if not env_issues else f'FAIL ({len(env_issues)} issues)'}")

    # 2. Exposed secrets
    secret_issues = check_exposed_secrets()
    results["checks"]["exposed_secrets"] = {"issues": secret_issues, "pass": len(secret_issues) == 0}
    results["issues"].extend(secret_issues)
    _log(f"  Secret scan: {'PASS' if not secret_issues else f'FAIL ({len(secret_issues)} found)'}")

    # 3. File integrity
    hashes = check_file_integrity()
    results["checks"]["file_integrity"] = {"hashes": hashes, "file_count": len(hashes)}
    _log(f"  File integrity: {len(hashes)} scripts hashed")

    # 4. Disk space
    disk = check_disk_space()
    results["checks"]["disk_space"] = disk
    _log(f"  Disk: {disk}")

    # 5. Open ports
    ports = check_open_ports()
    results["checks"]["open_ports"] = ports[:500]
    _log(f"  Ports checked")

    # 6. SSH attempts
    failed_ssh = check_ssh_auth()
    results["checks"]["failed_ssh"] = failed_ssh
    if failed_ssh > 20:
        results["issues"].append(f"High SSH failures: {failed_ssh}")
    _log(f"  SSH failed attempts: {failed_ssh}")

    # Overall severity
    if any("secret" in i.lower() or "key" in i.lower() for i in results["issues"]):
        results["severity"] = "CRITICAL"
    elif len(results["issues"]) > 3:
        results["severity"] = "WARNING"
    else:
        results["severity"] = "OK"

    _save_json(AUDIT_PATH, results)

    # Send alert if issues found
    if results["issues"]:
        try:
            import notifier
            msg = f"Severity: {results['severity']}\nIssues: {len(results['issues'])}\n"
            for i in results["issues"][:5]:
                msg += f"• {i}\n"
            notifier.send(msg,
                         notifier.AlertLevel.URGENT if results["severity"] == "CRITICAL" else notifier.AlertLevel.NORMAL,
                         title="Security Audit")
        except Exception:
            pass

    _log(f"Severity: {results['severity']}, Issues: {len(results['issues'])}")
    _log("=== AUDIT COMPLETE ===")
    return results


if __name__ == "__main__":
    run()
