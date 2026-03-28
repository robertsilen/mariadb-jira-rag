#!/usr/bin/env python3
"""
fetch_jira.py — Incremental fetcher for jira.mariadb.org MDEV issues.

Usage:
    python fetch_jira.py newest [N]       Fetch the N most recently updated issues (default: 100)
    python fetch_jira.py backfill [N]     Fetch N older issues starting from the oldest already downloaded (default: 500)
    python fetch_jira.py status           Show what's been fetched so far

Examples:
    python fetch_jira.py newest 1000      # first run: get the 1000 newest issues
    python fetch_jira.py backfill 2000    # then backfill 2000 older ones
    python fetch_jira.py backfill         # keep backfilling in batches of 500
    python fetch_jira.py newest 200       # daily: catch up on recent changes
"""

import requests
import json
import os
import sys
import time
import re
from pathlib import Path

DATA_DIR = Path("data/issues")
STATE_FILE = Path("data/fetch_state.json")

JIRA_BASE = "https://jira.mariadb.org"
SEARCH_URL = f"{JIRA_BASE}/rest/api/2/search"
PAGE_SIZE = 100  # Jira API max per request
RATE_LIMIT_SLEEP = 1  # seconds between requests, be polite

FIELDS = "summary,description,status,resolution,issuetype,priority,components,fixVersions,created,updated,comment"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_page(jql, start_at=0, max_results=PAGE_SIZE):
    """Fetch a single page of results from Jira."""
    params = {
        "jql": jql,
        "startAt": start_at,
        "maxResults": min(max_results, PAGE_SIZE),
        "fields": FIELDS,
    }
    resp = requests.get(SEARCH_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def save_issue(issue):
    """Save a single issue as a JSON file."""
    key = issue["key"]
    filepath = DATA_DIR / f"{key}.json"
    filepath.write_text(json.dumps(issue, indent=2, ensure_ascii=False))
    return key


def get_existing_keys():
    """Get set of MDEV keys already downloaded."""
    if not DATA_DIR.exists():
        return set()
    return {f.stem for f in DATA_DIR.glob("MDEV-*.json")}


def get_oldest_mdev_number():
    """Find the lowest MDEV number we have on disk."""
    keys = get_existing_keys()
    if not keys:
        return None
    numbers = []
    for k in keys:
        m = re.match(r"MDEV-(\d+)", k)
        if m:
            numbers.append(int(m.group(1)))
    return min(numbers) if numbers else None


def get_newest_mdev_number():
    """Find the highest MDEV number we have on disk."""
    keys = get_existing_keys()
    if not keys:
        return None
    numbers = []
    for k in keys:
        m = re.match(r"MDEV-(\d+)", k)
        if m:
            numbers.append(int(m.group(1)))
    return max(numbers) if numbers else None


def fetch_issues(jql, limit):
    """Fetch issues matching JQL, up to limit. Returns count of issues saved."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    start_at = 0
    total = None
    fetched = 0
    
    while fetched < limit:
        remaining = limit - fetched
        data = fetch_page(jql, start_at, remaining)
        
        if total is None:
            total = data["total"]
            print(f"  Query matches {total} issues, fetching up to {limit}")
        
        issues = data.get("issues", [])
        if not issues:
            break
        
        for issue in issues:
            if fetched >= limit:
                break
            key = save_issue(issue)
            fetched += 1
            if fetched % 50 == 0 or fetched == limit:
                print(f"  Saved {fetched}/{min(limit, total)} — latest: {key}")
        
        start_at += len(issues)
        
        if start_at >= total:
            break
        
        time.sleep(RATE_LIMIT_SLEEP)
    
    return fetched


def cmd_newest(limit=100):
    """Fetch the N most recently updated MDEV issues."""
    print(f"Fetching {limit} most recently updated MDEV issues...")
    jql = "project = MDEV ORDER BY updated DESC"
    count = fetch_issues(jql, limit)
    
    state = load_state()
    state["last_newest_fetch"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_newest_count"] = count
    save_state(state)
    
    print(f"Done. {count} issues saved to {DATA_DIR}/")


def cmd_backfill(limit=500):
    """Fetch N older issues, starting from below the oldest we have."""
    oldest = get_oldest_mdev_number()
    
    if oldest is None:
        print("No issues on disk yet. Run 'newest' first to seed the dataset.")
        print("  Example: python fetch_jira.py newest 1000")
        return
    
    if oldest <= 1:
        print(f"Already have MDEV-1. Nothing older to fetch.")
        return
    
    print(f"Oldest issue on disk: MDEV-{oldest}")
    print(f"Fetching up to {limit} issues with key < MDEV-{oldest}...")
    
    # Fetch issues with key before our oldest, ordered newest-first
    # so we fill in from just below our oldest downward
    jql = f'project = MDEV AND key < "MDEV-{oldest}" ORDER BY key DESC'
    count = fetch_issues(jql, limit)
    
    new_oldest = get_oldest_mdev_number()
    state = load_state()
    state["last_backfill_fetch"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_backfill_count"] = count
    state["oldest_key"] = f"MDEV-{new_oldest}" if new_oldest else None
    save_state(state)
    
    print(f"Done. {count} issues saved. Oldest on disk: MDEV-{new_oldest}")
    if new_oldest and new_oldest > 1:
        remaining_estimate = new_oldest - 1
        print(f"  ~{remaining_estimate} older issues remaining. Run 'backfill' again to continue.")


def cmd_status():
    """Show what's been fetched so far."""
    keys = get_existing_keys()
    
    if not keys:
        print("No issues downloaded yet.")
        print("  Start with: python fetch_jira.py newest 1000")
        return
    
    numbers = sorted(int(re.match(r"MDEV-(\d+)", k).group(1)) for k in keys if re.match(r"MDEV-(\d+)", k))
    
    print(f"Issues on disk:  {len(numbers)}")
    print(f"Key range:       MDEV-{numbers[0]} to MDEV-{numbers[-1]}")
    print(f"Disk usage:      {sum(f.stat().st_size for f in DATA_DIR.glob('MDEV-*.json')) / 1024 / 1024:.1f} MB")
    
    # Find gaps (ranges of missing keys)
    full_range = set(range(numbers[0], numbers[-1] + 1))
    missing = full_range - set(numbers)
    if missing:
        print(f"Gaps:            {len(missing)} missing keys in range")
    else:
        print(f"Gaps:            none (contiguous)")
    
    state = load_state()
    if state:
        print(f"\nFetch history:")
        if "last_newest_fetch" in state:
            print(f"  Last 'newest':   {state['last_newest_fetch']} ({state.get('last_newest_count', '?')} issues)")
        if "last_backfill_fetch" in state:
            print(f"  Last 'backfill': {state['last_backfill_fetch']} ({state.get('last_backfill_count', '?')} issues)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    if command == "newest":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 100
        cmd_newest(limit)
    
    elif command == "backfill":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 500
        cmd_backfill(limit)
    
    elif command == "status":
        cmd_status()
    
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
