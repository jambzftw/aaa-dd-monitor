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


def snapshot(fragment, field="filmAvailability"):
    """Return the endpoint body as stable JSON text, narrowed to one top-level key if present.

    For the film availability endpoint this keeps only 'filmAvailability'
    (status, booking periods) and ignores the static attribute lookup tables.
    """
    for r in capture_json():
        if fragment in r["url"]:
            body = r["body"]
            if field and isinstance(body, dict) and field in body:
                body = body[field]
            return json.dumps(body, sort_keys=True)
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
        try:
            data = json.loads(snap)
            detail = (f" Status: {', '.join(data.get('categories', []))}; "
                      f"booking periods: {len(data.get('advanceBookingPeriods', []))}.")
        except Exception:
            detail = ""
        notify("Ayala All Access: Avengers: Doomsday availability changed."
               f"{detail} Check {FILM_URL}")
    else:
        print("No change.")
    state[fragment] = digest
    json.dump(state, open(STATE_FILE, "w"))


API = "https://digital-api.ayalaallaccess.com/ocapi/v1"
FILM_ID = "HO00000111"
TARGET_DATES = [d.strip() for d in
                (os.environ.get("AAA_DATES") or "2026-12-18,2026-12-19").split(",") if d.strip()]
EVENING_FROM = os.environ.get("AAA_EVENING_FROM") or "17:00"   # HH:MM, local cinema time
SKIP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "cookie"}


def fetch_showtimes():
    """Open the film page once to pick up the site's own API headers, then query
    showtimes for each target date from inside the same browser session.
    Returns (response_bodies, site_names, request_log)."""
    api_headers, site_names, bodies, log = {}, {}, [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()

        def on_request(req):
            if not api_headers and "digital-api.ayalaallaccess.com" in req.url:
                api_headers.update({k: v for k, v in req.headers.items()
                                    if k.lower() not in SKIP_HEADERS})

        def on_response(resp):
            if resp.url.split("?")[0].rstrip("/").endswith("/ocapi/v1/sites"):
                try:
                    for s in resp.json().get("sites", []):
                        site_names[s["id"]] = s["name"]["text"]
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(FILM_URL, wait_until="networkidle")
        page.wait_for_timeout(5000)

        ids = list(site_names)
        for date in TARGET_DATES:
            base = f"{API}/showtimes/by-business-date/{date}?filmIds={FILM_ID}"
            resp = ctx.request.get(base + "".join(f"&siteIds={i}" for i in ids),
                                   headers=api_headers)
            log.append((resp.status, date, "all sites"))
            if resp.ok:
                bodies.append(resp.json())
                continue
            for sid in ids:  # fallback: one request per cinema
                r = ctx.request.get(f"{base}&siteIds={sid}", headers=api_headers)
                log.append((r.status, date, sid))
                if r.ok:
                    bodies.append(r.json())
                elif r.status in (401, 403, 404):
                    break
        browser.close()
    return bodies, site_names, log


def parse_slots(bodies, site_names):
    """Flatten showtime responses into {showtime_id: details}."""
    slots = {}
    for body in bodies:
        if not isinstance(body, dict):
            continue
        attrs = {a["id"]: a["name"]["text"]
                 for a in (body.get("relatedData") or {}).get("attributes", [])}
        for st in body.get("showtimes", []):
            if st.get("filmId") not in (None, FILM_ID):
                continue
            starts = (st.get("schedule") or {}).get("startsAt") or st.get("startsAt") or ""
            if len(starts) < 16:
                continue
            sid = st.get("siteId")
            slots[st.get("id") or f"{sid}-{starts}"] = {
                "site": site_names.get(sid, sid),
                "date": starts[:10],
                "time": starts[11:16],
                "formats": ", ".join(attrs.get(a, a) for a in st.get("attributeIds", [])),
            }
    return slots


def wanted(slots):
    """Keep only target dates, evening start times."""
    return {k: v for k, v in slots.items()
            if v["date"] in TARGET_DATES and v["time"] >= EVENING_FROM}


def fmt_slot(s):
    extra = f" ({s['formats']})" if s["formats"] else ""
    return f"{s['date'][5:]} {s['time']} {s['site']}{extra}"


def slots_cmd():
    """Print what the site returns right now (use this to verify the filter)."""
    bodies, site_names, log = fetch_showtimes()
    for status, date, scope in log:
        print(f"[{status}] {date} {scope}")
    with open("aaa_slots_raw.json", "w") as f:
        json.dump(bodies, f, indent=2)
    allslots = parse_slots(bodies, site_names)
    print(f"\n{len(allslots)} showtimes returned for {', '.join(TARGET_DATES)}")
    hits = wanted(allslots)
    print(f"{len(hits)} match evening filter (start >= {EVENING_FROM}):")
    for s in sorted(hits.values(), key=lambda x: (x["date"], x["time"], str(x["site"]))):
        print("  " + fmt_slot(s))
    if not bodies:
        print("\nNo showtime data came back. Check the status codes above; "
              "aaa_slots_raw.json is empty.")


def check_slots():
    """One-shot run for cron/Actions: alert on new evening slots on the target dates."""
    bodies, site_names, log = fetch_showtimes()
    if not bodies:
        print(f"No showtime data returned: {log[:5]}")
        sys.exit(1)
    hits = wanted(parse_slots(bodies, site_names))
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    prev = state.get("slots")
    ordered = sorted(hits.items(), key=lambda kv: (kv[1]["date"], kv[1]["time"], str(kv[1]["site"])))
    if prev is None:
        print(f"Baseline saved: {len(hits)} matching slots.")
        if hits:
            notify(f"Ayala All Access: {len(hits)} evening slots already open for "
                   f"Avengers: Doomsday on {', '.join(TARGET_DATES)}:\n"
                   + "\n".join(fmt_slot(v) for _, v in ordered[:10]))
    else:
        new = [(k, v) for k, v in ordered if k not in set(prev)]
        if new:
            notify(f"Ayala All Access: {len(new)} new evening slot(s) for Avengers: Doomsday:\n"
                   + "\n".join(fmt_slot(v) for _, v in new[:10]) + f"\n{FILM_URL}")
        else:
            print("No new slots.")
    state["slots"] = sorted(hits)
    json.dump(state, open(STATE_FILE, "w"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover")
    sub.add_parser("slots")
    sub.add_parser("check-slots")
    c = sub.add_parser("check")
    c.add_argument("fragment", help="part of the JSON endpoint URL to monitor")
    w = sub.add_parser("watch")
    w.add_argument("fragment", help="part of the JSON endpoint URL to monitor")
    w.add_argument("--interval", type=int, default=600, help="seconds between checks")
    args = ap.parse_args()
    if args.cmd == "discover":
        discover()
    elif args.cmd == "slots":
        slots_cmd()
    elif args.cmd == "check-slots":
        check_slots()
    elif args.cmd == "check":
        check(args.fragment)
    else:
        watch(args.fragment, args.interval)
