#!/usr/bin/env python3
"""Refresh recent ProSportsTransactions data and the web app snapshot.

From contract_history:
    ../web_app/venv/bin/python refresh_recent.py

Starts a detached Unflare container if port 5002 is not already in use.
If Unflare cannot fetch the site, retries with a visible Chrome window.
Then rebuilds the player crosswalk and uses the existing refresh runner's
checked build and snapshot sync.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from dateutil.relativedelta import relativedelta

HIST = Path(__file__).resolve().parent
ROOT = HIST.parent
sys.path.insert(0, str(ROOT))
import refresh_contract_data as pipeline

CONTAINER = "contract-history-unflare"
IMAGE = "ghcr.io/iamyegor/unflare"


def port_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 5002), timeout=1):
            return True
    except OSError:
        return False


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def ensure_unflare() -> bool:
    """Return True only if this run started the service and should stop it."""
    if port_ready():
        print("Using the service already listening on localhost:5002.", flush=True)
        return False

    existing = docker("inspect", "--format", "{{.State.Running}}", CONTAINER, check=False)
    started_here = False
    if existing.returncode == 0:
        if existing.stdout.strip() != "true":
            docker("start", CONTAINER)
            started_here = True
    elif "no such object" in existing.stderr.lower() or "no such container" in existing.stderr.lower():
        docker("run", "-d", "--rm", "--name", CONTAINER,
               "-p", "127.0.0.1:5002:5002", IMAGE)
        started_here = True
    else:
        raise RuntimeError(f"Could not inspect Docker container: {existing.stderr.strip()}")

    print(f"Started {CONTAINER}; waiting for localhost:5002...", flush=True)
    for _ in range(120):
        if port_ready():
            return started_here
        time.sleep(0.5)
    logs = docker("logs", "--tail", "20", CONTAINER, check=False).stderr.strip()
    if started_here:
        docker("stop", CONTAINER, check=False)
    raise RuntimeError(f"Unflare did not become ready within 60 seconds. {logs}")


def refresh(since: date, through: date, modern: str | None,
            no_tests: bool, browser: bool = False) -> None:
    py = sys.executable
    pipeline.banner(f"ProSportsTransactions: {since} through {through}")
    scrape_cmd = [py, "psx_contracts_v2.py", "--since", since.isoformat(),
                  "--through", through.isoformat()]
    if browser:
        pipeline.need(pipeline.run([*scrape_cmd, "--browser"], HIST), "PST browser scrape")
    else:
        result = pipeline.run(scrape_cmd, HIST)
        if result.returncode:
            logs = docker("logs", "--tail", "25", CONTAINER, check=False)
            detail = (logs.stdout + logs.stderr).strip() if logs.returncode == 0 else ""
            if detail:
                print(f"\nUnflare logs (last 25 lines):\n{detail}", file=sys.stderr)
            print("\nUnflare could not fetch the results. Retrying in visible Chrome; "
                  "solve the Cloudflare challenge if it appears.", file=sys.stderr, flush=True)
            pipeline.need(pipeline.run([*scrape_cmd, "--browser"], HIST), "PST browser scrape")

    pipeline.banner("Player crosswalk and trade/movement resolution")
    cmd = [py, "psx_crosswalk.py"] + ([modern] if modern else [])
    pipeline.need(pipeline.run(cmd, HIST), "psx_crosswalk.py")

    pipeline.step_build(SimpleNamespace(modern=modern, psx=True, dry_run=False))
    pipeline.step_sync()
    if not no_tests:
        pipeline.step_tests()
    pipeline.step_summary()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--months", type=int, default=2,
                        help="Calendar months before --through to start the refresh (default: 2).")
    window.add_argument("--days", type=int,
                        help="Use a number of days instead of calendar months.")
    parser.add_argument("--through", type=date.fromisoformat, default=date.today(),
                        metavar="YYYY-MM-DD", help="Inclusive end date (default: today).")
    parser.add_argument("--modern", type=Path, help="Local modern.csv, if not using the remote source.")
    parser.add_argument("--browser", action="store_true",
                        help="Use visible Chrome directly, without starting Unflare.")
    parser.add_argument("--no-tests", action="store_true", help="Skip the pipeline's test suites.")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("--days must be positive")
    if args.months < 1:
        parser.error("--months must be positive")
    modern = str(args.modern.resolve()) if args.modern else None
    since = (args.through - timedelta(days=args.days) if args.days is not None
             else args.through - relativedelta(months=args.months))

    started_here = False
    try:
        if not args.browser:
            started_here = ensure_unflare()
        refresh(since, args.through, modern, args.no_tests, args.browser)
    except (RuntimeError, OSError, pipeline.Stop) as exc:
        print(f"\nSTOPPED: {exc}", file=sys.stderr)
        return 1
    finally:
        if started_here:
            stopped = docker("stop", CONTAINER, check=False)
            if stopped.returncode:
                print(f"WARNING: could not stop {CONTAINER}: {stopped.stderr.strip()}", file=sys.stderr)
    print("\nRecent contract refresh and web app snapshot sync complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
