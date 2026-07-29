#!/usr/bin/env python3
"""
YSSY Special Livery Watcher
---------------------------
Reads a list of tracked aircraft registrations from a Google Sheet,
checks their live positions via OpenSky, resolves each airborne
aircraft's destination airport via hexdb.io, and sends a push
notification (via ntfy.sh) the first time a tracked aircraft is
found heading to the target airport.

Designed to be run on a schedule (e.g. every 5 minutes via GitHub
Actions cron). State is kept in small JSON files so repeat runs
don't spam duplicate notifications.
"""

import csv
import io
import json
import os
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# CONFIG - edit these or set as environment variables / GitHub Secrets
# ---------------------------------------------------------------------------

# Publish your Google Sheet as CSV: File > Share > Publish to web > CSV
# Sheet must have a column called "registration" (and optionally "notes")
GOOGLE_SHEET_CSV_URL = os.environ.get("GOOGLE_SHEET_CSV_URL", "")

# ntfy.sh topic - pick any unique, hard-to-guess name. No account needed.
# Install the ntfy app on Android and subscribe to this same topic name.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

TARGET_AIRPORT_ICAO = "YSSY"

# How many hours before we're willing to re-notify about the same aircraft
# heading to the same airport again (covers next day's flight, etc.)
RENOTIFY_AFTER_HOURS = 20

CACHE_FILE = Path("icao24_cache.json")     # registration -> icao24 hex
NOTIFIED_FILE = Path("notified.json")       # icao24 -> last notified timestamp

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
HEXDB_REG_TO_HEX = "https://hexdb.io/reg-hex?reg={reg}"
HEXDB_CALLSIGN_DEST = "https://hexdb.io/callsign-des_icao?callsign={cs}"

HEADERS = {"User-Agent": "yssy-livery-watcher/1.0"}


# ---------------------------------------------------------------------------

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2))


def fetch_registrations():
    """Pull the current watchlist from the published Google Sheet CSV."""
    if not GOOGLE_SHEET_CSV_URL:
        raise SystemExit("GOOGLE_SHEET_CSV_URL is not set.")
    resp = requests.get(GOOGLE_SHEET_CSV_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    rows = []
    for row in reader:
        reg = (row.get("registration") or "").strip().upper()
        if reg:
            rows.append({"registration": reg, "notes": (row.get("notes") or "").strip()})
    return rows


def get_icao24(registration, cache):
    """Look up (and cache) the ICAO24 hex for a registration via hexdb.io."""
    if registration in cache:
        return cache[registration]
    url = HEXDB_REG_TO_HEX.format(reg=registration)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        hexcode = resp.text.strip().lower()
        if resp.status_code == 200 and hexcode and "not found" not in hexcode.lower():
            cache[registration] = hexcode
            return hexcode
    except requests.RequestException:
        pass
    return None


def get_destination(callsign):
    """Resolve a callsign to its destination airport ICAO code, if known."""
    callsign = callsign.strip()
    if not callsign:
        return None
    url = HEXDB_CALLSIGN_DEST.format(cs=callsign)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            dest = resp.text.strip().upper()
            if dest and "not found" not in dest.lower() and len(dest) == 4:
                return dest
    except requests.RequestException:
        pass
    return None


def fetch_opensky_states():
    resp = requests.get(OPENSKY_STATES_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("states") or []


def send_ntfy(title, message):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set - skipping push notification:", title, message)
        return
    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "airplane"},
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"ntfy notification sent OK: {title}")
        else:
            print(f"ntfy notification FAILED (status {resp.status_code}): {resp.text}")
    except requests.RequestException as e:
        print(f"ntfy notification FAILED (exception): {e}")


def main():
    target_airport = TARGET_AIRPORT_ICAO.strip().upper()
    print(f"Target airport is: '{target_airport}' (length {len(target_airport)})")

    icao24_cache = load_json(CACHE_FILE, {})
    notified = load_json(NOTIFIED_FILE, {})

    registrations = fetch_registrations()
    print(f"Loaded {len(registrations)} tracked registrations.")

    # Build reg -> icao24 map, looking up any we don't have cached yet
    tracked = {}
    for entry in registrations:
        reg = entry["registration"]
        hexcode = get_icao24(reg, icao24_cache)
        if hexcode:
            tracked[hexcode] = entry
        else:
            print(f"Could not resolve ICAO24 for registration {reg}")
        time.sleep(0.2)  # be polite to hexdb.io

    save_json(CACHE_FILE, icao24_cache)

    if not tracked:
        print("No trackable aircraft this run.")
        return

    states = fetch_opensky_states()
    print(f"OpenSky returned {len(states)} live aircraft states.")

    now = time.time()
    for state in states:
        icao24 = (state[0] or "").strip().lower()
        if icao24 not in tracked:
            continue

        callsign = (state[1] or "").strip()
        on_ground = state[8]
        if on_ground or not callsign:
            continue

        entry = tracked[icao24]
        reg = entry["registration"]

        last_notified = notified.get(icao24)
        if last_notified and (now - last_notified) < RENOTIFY_AFTER_HOURS * 3600:
            continue  # already alerted recently for this aircraft

        dest = get_destination(callsign)
        print(f"{reg} ({callsign}) -> destination {dest!r} (comparing to target {target_airport!r})")

        if dest == target_airport:
            notes = f" ({entry['notes']})" if entry["notes"] else ""
            send_ntfy(
                title=f"{reg} inbound to {target_airport}",
                message=f"{reg}{notes} - callsign {callsign} - heading to {target_airport}",
            )
            notified[icao24] = now

        time.sleep(0.2)  # be polite to hexdb.io

    save_json(NOTIFIED_FILE, notified)


if __name__ == "__main__":
    main()
