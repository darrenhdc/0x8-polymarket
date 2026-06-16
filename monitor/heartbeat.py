#!/usr/bin/env python3
"""Heartbeat monitor — health checks and daily summary for the weather trading system.

Checks:
  1. Portfolio file freshness (data/portfolio.json)
  2. Polymarket API reachability
  3. Open-Meteo API reachability
  4. Signal history freshness
  5. Calibration data existence

Outputs:
  - CLI text summary (default)
  - JSON (--json)
  - HTTP endpoint (--serve PORT)

Usage::
    python3 -m monitor.heartbeat                    # CLI summary
    python3 -m monitor.heartbeat --json             # JSON output
    python3 -m monitor.heartbeat --serve 8080       # HTTP server
    python3 -m monitor.heartbeat --check signals    # Single check
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CHECKS = {
    "portfolio": {"max_age_seconds": 3600, "path": DATA_DIR / "portfolio.json"},
    "signal_history": {"max_age_seconds": 86400, "path": DATA_DIR / "signal_history.db"},
    "calibration": {"max_age_seconds": 604800, "path": DATA_DIR / "calibration.json"},
}

API_ENDPOINTS = {
    "polymarket": "https://gamma-api.polymarket.com",
    "open_meteo": "https://api.open-meteo.com/v1/forecast?latitude=22.3&longitude=114.2&current_weather=true",
}

# ---------------------------------------------------------------------------
# Health check implementations
# ---------------------------------------------------------------------------

def check_file_freshness(path: Path, max_age_seconds: int) -> dict:
    """Check if a file exists and is fresh."""
    if not path.exists():
        return {"status": "FAIL", "reason": f"File not found: {path}"}

    mtime = path.stat().st_mtime
    age_seconds = time.time() - mtime
    if age_seconds > max_age_seconds:
        return {
            "status": "WARN",
            "reason": f"Stale: {age_seconds:.0f}s old (max {max_age_seconds}s)",
            "age_seconds": round(age_seconds, 1),
        }

    return {
        "status": "OK",
        "age_seconds": round(age_seconds, 1),
    }


def check_api_reachability(name: str, url: str, timeout: int = 10) -> dict:
    """Check if an API endpoint is reachable."""
    try:
        import urllib.request
        # Use GET with short read to avoid 405 on HEAD
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "polymarket-weather-heartbeat/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            if status < 400:
                return {"status": "OK", "http_status": status}
            return {"status": "WARN", "reason": f"HTTP {status}", "http_status": status}
    except Exception as exc:
        return {"status": "FAIL", "reason": str(exc)}


def check_signals() -> dict:
    """Check if today's signals exist in signal_history.db."""
    db_path = DATA_DIR / "signal_history.db"
    if not db_path.exists():
        return {"status": "FAIL", "reason": f"DB not found: {db_path}"}

    try:
        import sqlite3
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*) FROM signal_history WHERE run_date = ?",
            (today,)
        ).fetchone()
        count = row[0] if row else 0
        conn.close()
        if count == 0:
            return {"status": "WARN", "reason": f"No signals for {today}"}
        return {"status": "OK", "signals_today": count}
    except Exception as exc:
        return {"status": "FAIL", "reason": str(exc)}


def run_all_checks() -> dict:
    """Run the full health check suite."""
    results: Dict[str, Any] = {}
    overall_ok = True
    overall_warn = False

    # File freshness checks
    for name, cfg in CHECKS.items():
        results[name] = check_file_freshness(cfg["path"], cfg["max_age_seconds"])
        if results[name]["status"] == "FAIL":
            overall_ok = False
        elif results[name]["status"] == "WARN":
            overall_warn = True

    # API checks
    for name, url in API_ENDPOINTS.items():
        results[name] = check_api_reachability(name, url)
        if results[name]["status"] == "FAIL":
            overall_ok = False
        elif results[name]["status"] == "WARN":
            overall_warn = True

    # Signal check
    results["signals"] = check_signals()
    if results["signals"]["status"] == "FAIL":
        overall_ok = False
    elif results["signals"]["status"] == "WARN":
        overall_warn = True

    # Overall status
    if overall_ok and not overall_warn:
        status = "HEALTHY"
    elif overall_ok:
        status = "DEGRADED"
    else:
        status = "UNHEALTHY"

    return {
        "status": status,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "checks": results,
    }

# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_cli(report: dict) -> str:
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"HEARTBEAT — {report['timestamp']}")
    lines.append(f"Overall: {report['status']}")
    lines.append(f"{'='*60}")

    for name, result in report["checks"].items():
        icon = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌"}.get(result["status"], "❓")
        lines.append(f"  {icon} {name:<20} {result['status']}")
        if "reason" in result:
            lines.append(f"      → {result['reason']}")
        if "signals_today" in result:
            lines.append(f"      → {result['signals_today']} signals today")

    return "\n".join(lines)


def format_json(report: dict) -> str:
    return json.dumps(report, indent=2)

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class HeartbeatHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress default logging
        pass

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            report = run_all_checks()
            status_code = 200 if report["status"] == "HEALTHY" else 503
            body = json.dumps(report, indent=2)
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()


def serve(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), HeartbeatHandler)
    print(f"[heartbeat] Serving on http://0.0.0.0:{port}/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[heartbeat] Shutting down")
        server.shutdown()

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(
    json_mode: bool = False,
    serve_port: Optional[int] = None,
    single_check: Optional[str] = None,
) -> int:
    if serve_port:
        serve(serve_port)
        return 0

    if single_check:
        if single_check == "signals":
            result = check_signals()
        elif single_check in CHECKS:
            cfg = CHECKS[single_check]
            result = check_file_freshness(cfg["path"], cfg["max_age_seconds"])
        elif single_check in API_ENDPOINTS:
            result = check_api_reachability(single_check, API_ENDPOINTS[single_check])
        else:
            print(f"Unknown check: {single_check}", file=sys.stderr)
            return 1

        if json_mode:
            print(json.dumps({single_check: result}, indent=2))
        else:
            icon = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌"}.get(result["status"], "❓")
            print(f"{icon} {single_check}: {result['status']}")
            if "reason" in result:
                print(f"   → {result['reason']}")
        return 0 if result["status"] != "FAIL" else 1

    report = run_all_checks()
    if json_mode:
        print(format_json(report))
    else:
        print(format_cli(report))

    return 0 if report["status"] != "UNHEALTHY" else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Heartbeat monitor for weather trading system")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--serve", type=int, default=None, metavar="PORT", help="Start HTTP server")
    parser.add_argument("--check", default=None, help="Run single check (portfolio, signals, polymarket, open_meteo)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(main(json_mode=args.json, serve_port=args.serve, single_check=args.check))
