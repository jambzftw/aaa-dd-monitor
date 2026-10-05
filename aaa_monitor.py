#!/usr/bin/env python3
"""
Ayala All Access schedule monitor.

Setup:
    pip install playwright requests
    playwright install chromium

Step 1 - find the schedule endpoint:
    python aaa_monitor.py discover

Step 2 - watch it (use a URL fragment from the discover output):
    python aaa_monitor.py watch "schedule" --interval 600

Optional alerts: set NTFY_TOPIC (https://ntfy.sh) and you get a phone push on changes.
"""
import argparse
import hashlib
import json
import os
import sys
import time

import requests
from playwright.sync_api import sync_playwright

FILM_URL = "https://www.ayalaallaccess.com/films/Avengers-Doomsday/HO00000111"
STATE_FILE = "aaa_state.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def capture_json(url=FILM_URL, wait_ms=8000):
    """Load the page in a headless browser and return all JSON responses seen."""
    found = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=UA).new_page()

        def on_response(resp):
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                try:
                    found.append({"url": resp.url, "status": resp.status, "body": resp.json()})
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(wait_ms)
        browser.close()
    return found


def discover():
    responses = capture_json()
    if not responses:
        print("No JSON responses captured. The site may block headless browsers "
              "or render server-side; open DevTools > Network > Fetch/XHR manually.")
        return
    with open("aaa_discovery.json", "w") as f:
        json.dump(responses, f, indent=2)
    print(f"Captured {len(responses)} JSON responses (full bodies in aaa_discovery.json):\n")
    for r in responses:
        size = len(json.dumps(r["body"]))
        print(f"[{r['status']}] {size:>8} bytes  {r['url']}")
    print("\nPick the URL that contains showtimes/cinemas and pass a unique part "
          "of it to the 'watch' command.")


def notify(msg):
    print(msg)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"), timeout=10)
        except Exception as e:
            print(f"ntfy failed: {e}")


def snapshot(fragment):
    for r in capture_json():
        if fragment in r["url"]:
            return json.dumps(r["body"], sort_keys=True)
    return None


def watch(fragment, interval):
    last = None
    if os.path.exists(STATE_FILE):
        last = json.load(open(STATE_FILE)).get(fragment)
    while True:
        try:
            snap = snapshot(fragment)
            if snap is None:
                print("Endpoint not seen this run (blocked or changed).")
            else:
                digest = hashlib.sha256(snap.encode()).hexdigest()
                if last is None:
                    print("Baseline saved.")
                elif digest != last:
                    notify("Ayala All Access: schedule data changed for Avengers: Doomsday. "
                           f"Check {FILM_URL}")
                else:
                    print(time.strftime("%H:%M:%S"), "no change")
                last = digest
                json.dump({fragment: digest}, open(STATE_FILE, "w"))
        except Exception as e:
            print(f"Run failed: {e}")
        time.sleep(interval)


def check(fragment):
    """One-shot run for cron/Actions: compare against saved state, alert, exit."""
    snap = snapshot(fragment)
    if snap is None:
        print("Endpoint not seen this run (blocked or changed).")
        sys.exit(1)
    digest = hashlib.sha256(snap.encode()).hexdigest()
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    last = state.get(fragment)
    if last is None:
        print("Baseline saved.")
    elif digest != last:
        notify("Ayala All Access: schedule data changed for Avengers: Doomsday. "
               f"Check {FILM_URL}")
    else:
        print("No change.")
    state[fragment] = digest
    json.dump(state, open(STATE_FILE, "w"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover")
    c = sub.add_parser("check")
    c.add_argument("fragment", help="part of the JSON endpoint URL to monitor")
    w = sub.add_parser("watch")
    w.add_argument("fragment", help="part of the JSON endpoint URL to monitor")
    w.add_argument("--interval", type=int, default=600, help="seconds between checks")
    args = ap.parse_args()
    if args.cmd == "discover":
        discover()
    elif args.cmd == "check":
        check(args.fragment)
    else:
        watch(args.fragment, args.interval)
