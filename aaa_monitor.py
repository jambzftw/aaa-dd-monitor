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
import datetime as dt
import hashlib
import re
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


def notify(msg, title=None, priority=None, tags=None):
    print(msg)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        headers = {"Click": FILM_URL}      # tapping the notification opens the film page
        if title:
            headers["Title"] = title
        if priority:
            headers["Priority"] = priority
        if tags:
            headers["Tags"] = tags         # ntfy turns these into emoji, e.g. "tada"
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"),
                          headers=headers, timeout=10)
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
# Cinema site IDs (from the site's /sites list): Glorietta, Greenbelt, Ayala Malls Circuit (Makati)
TARGET_SITES = [s.strip() for s in
                (os.environ.get("AAA_SITES") or "1001,1003,1020").split(",") if s.strip()]
SITE_LABELS = {"1001": "Glorietta", "1003": "Greenbelt", "1020": "Circuit Makati"}
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

        ids = TARGET_SITES
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
        related = body.get("relatedData") or {}
        attrs = {a["id"]: a["name"]["text"] for a in related.get("attributes", [])}
        screens = {s["id"]: s["name"]["text"] for s in related.get("screens", [])}
        for st in body.get("showtimes", []):
            if st.get("filmId") not in (None, FILM_ID):
                continue
            sched = st.get("schedule") or {}
            starts = sched.get("startsAt") or st.get("startsAt") or ""
            if len(starts) < 16:
                continue
            sid = st.get("siteId")
            slots[st.get("id") or f"{sid}-{starts}"] = {
                "site_id": sid,
                "site": SITE_LABELS.get(sid) or site_names.get(sid, sid),
                "screen": screens.get(st.get("screenId"), ""),
                "date": sched.get("businessDate") or starts[:10],
                "time": starts[11:16],
                "sold_out": bool(st.get("isSoldOut")),
                # Drop internal screen codes like C1/C5; keep formats (2D, 4DX, ATMOS, ...)
                "formats": ", ".join(
                    n for n in (attrs.get(a, a) for a in st.get("attributeIds", []))
                    if not re.fullmatch(r"C\d+", n)),
            }
    return slots


def wanted(slots):
    """Keep only target cinemas, target dates, evening start times."""
    return {k: v for k, v in slots.items()
            if v["site_id"] in TARGET_SITES
            and v["date"] in TARGET_DATES and v["time"] >= EVENING_FROM}


def fmt_date(d):
    x = dt.date.fromisoformat(d)
    return f"{x.strftime('%a %b')} {x.day}"            # e.g. "Fri Dec 18"


def fmt_time(t):
    h, m = int(t[:2]), t[3:5]
    return f"{h % 12 or 12}:{m} {'AM' if h < 12 else 'PM'}"   # e.g. "7:00 PM"


def fmt_line(s, tag=True):
    screen = re.sub(r"^\w+\s+(?=Cinema)", "", s.get("screen") or "")   # "GL Cinema 5" -> "Cinema 5"
    where = ", ".join(x for x in (s["site"], screen) if x)
    extra = f" · {s['formats']}" if s["formats"] else ""
    sold = "  [SOLD OUT]" if tag and s.get("sold_out") else ""
    return f"{fmt_time(s['time'])}  {where}{extra}{sold}"


def fmt_slot(s, tag=True):
    return f"{fmt_date(s['date'])}  {fmt_line(s, tag)}"


def fmt_groups(slots):
    """Slots grouped under a date heading, one line per slot."""
    out, cur = [], None
    for s in slots:
        if s["date"] != cur:
            if cur is not None:
                out.append("")
            cur = s["date"]
            out.append(fmt_date(cur))
        out.append("  " + fmt_line(s))
    return out


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
    if isinstance(prev, list):            # oldest state format: ids only
        prev = {k: {"sold_out": False, "label": k} for k in prev}
    elif isinstance(prev, dict):          # tolerate {id: bool} from the previous version
        prev = {k: (v if isinstance(v, dict) else {"sold_out": bool(v), "label": k})
                for k, v in prev.items()}
    ordered = sorted(hits.items(), key=lambda kv: (kv[1]["date"], kv[1]["time"], str(kv[1]["site"])))
    # If a date's response is missing this run, don't treat its slots as removed.
    got_dates = {b.get("businessDate") for b in bodies if isinstance(b, dict)}
    complete = set(TARGET_DATES) <= got_dates

    if prev is None:
        print(f"Baseline saved: {len(hits)} matching slots.")
        if hits:
            open_n = sum(1 for v in hits.values() if not v["sold_out"])
            notify(f"{len(hits)} evening slots listed, {open_n} with seats. "
                   "You'll be alerted when this changes.\n\n"
                   + "\n".join(fmt_groups([v for _, v in ordered])),
                   title="Avengers: Doomsday monitor started", tags="movie_camera")
    else:
        new = [(k, v) for k, v in ordered if k not in prev]
        reopened = [(k, v) for k, v in ordered
                    if k in prev and prev[k]["sold_out"] and not v["sold_out"]]
        soldout = ([(k, v) for k, v in ordered
                    if k in prev and not prev[k]["sold_out"] and v["sold_out"]]
                   if os.environ.get("AAA_ALERT_SOLDOUT") else [])
        gone = [prev[k]["label"] for k in prev if k not in hits] if complete else []

        lines = []

        def section(head, body):
            if lines:
                lines.append("")
            lines.append(head)
            lines.extend(body)

        if new:
            section("NEW SLOT" + ("S" if len(new) > 1 else ""),
                    fmt_groups([v for _, v in new[:10]]))
        if reopened:
            section("SEATS OPENED UP", fmt_groups([v for _, v in reopened[:10]]))
        if soldout:
            section("JUST SOLD OUT", fmt_groups([v for _, v in soldout[:10]]))
        if gone:
            section("REMOVED", [f"  {g}" for g in gone[:10]])
        if lines:
            urgent = bool(new or reopened)
            notify("\n".join(lines),
                   title="Avengers: Doomsday - new slot" if new else "Avengers: Doomsday - schedule change",
                   priority="high" if urgent else None,
                   tags="tada" if new else ("ticket" if reopened else "warning"))
        else:
            print("No changes.")

    state["slots"] = {k: {"sold_out": v["sold_out"], "label": fmt_slot(v, tag=False)}
                      for k, v in hits.items()}
    if not complete and prev:             # keep old entries for dates that didn't come back
        for k, v in prev.items():
            state["slots"].setdefault(k, v)
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
