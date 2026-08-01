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
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from icao_nnumber_converter_us import n_to_icao

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

# Coordinates of the target airport, used only for the rough ETA estimate
# in notifications (straight-line distance / current ground speed - not a
# real flight-plan ETA, just a helpful approximation).
TARGET_AIRPORT_LAT = -33.9461
TARGET_AIRPORT_LON = 151.1772

# Timezone to display the estimated arrival time in
DISPLAY_TZ = ZoneInfo("Australia/Sydney")

# How many hours before we're willing to re-notify about the same aircraft
# heading to the same airport again (covers next day's flight, etc.)
RENOTIFY_AFTER_HOURS = 20

CACHE_FILE = Path("icao24_cache.json")     # registration -> icao24 hex
NOTIFIED_FILE = Path("notified.json")       # icao24 -> last notified timestamp

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
HEXDB_REG_TO_HEX = "https://hexdb.io/reg-hex?reg={reg}"
HEXDB_CALLSIGN_DEST = "https://hexdb.io/callsign-des_icao?callsign={cs}"
HEXDB_CALLSIGN_ORIGIN = "https://hexdb.io/callsign-origin_icao?callsign={cs}"
ADSBDB_CALLSIGN = "https://api.adsbdb.com/v0/callsign/{cs}"
ADSBDB_AIRCRAFT = "https://api.adsbdb.com/v0/aircraft/{reg}"

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

    last_error = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(GOOGLE_SHEET_CSV_URL, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(resp.text))
            rows = []
            for row in reader:
                reg = (row.get("registration") or "").strip().upper()
                if reg:
                    rows.append({"registration": reg, "notes": (row.get("notes") or "").strip()})
            return rows
        except requests.RequestException as e:
            last_error = e
            print(f"Attempt {attempt}/3 to fetch Google Sheet failed: {e}")
            if attempt < 3:
                time.sleep(5)

    raise SystemExit(f"Could not fetch Google Sheet after 3 attempts: {last_error}")


def get_icao24(registration, cache):
    """Look up (and cache) the ICAO24 hex for a registration."""
    if registration in cache:
        return cache[registration]

    # US "N-number" registrations map to their ICAO24 hex via a fixed,
    # publicly documented formula - no lookup needed, and it can't go stale.
    if registration.startswith("N"):
        try:
            hexcode = n_to_icao(registration).strip().lower()
            if hexcode:
                cache[registration] = hexcode
                return hexcode
        except (ValueError, KeyError):
            pass  # not a valid N-number format - fall through to lookups

    # Try hexdb.io next
    url = HEXDB_REG_TO_HEX.format(reg=registration)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        hexcode = resp.text.strip().lower()
        if resp.status_code == 200 and hexcode and "not found" not in hexcode.lower():
            cache[registration] = hexcode
            return hexcode
    except requests.RequestException:
        pass

    # Fall back to adsbdb.com, which draws from a different registry dataset
    # and often has aircraft (especially non-US/UK registries) hexdb.io lacks.
    try:
        resp = requests.get(
            ADSBDB_AIRCRAFT.format(reg=registration), headers=HEADERS, timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            hexcode = data.get("response", {}).get("aircraft", {}).get("mode_s")
            if hexcode:
                hexcode = hexcode.strip().lower()
                cache[registration] = hexcode
                return hexcode
    except (requests.RequestException, ValueError):
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


def get_origin(callsign):
    """Resolve a callsign to its departure airport ICAO code, if known."""
    callsign = callsign.strip()
    if not callsign:
        return None

    # Try hexdb.io first
    url = HEXDB_CALLSIGN_ORIGIN.format(cs=callsign)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            origin = resp.text.strip().upper()
            if origin and "not found" not in origin.lower() and len(origin) == 4:
                return origin
    except requests.RequestException:
        pass

    # Fall back to adsbdb.com, which draws from a broader route dataset and
    # sometimes has origin data hexdb.io is missing.
    try:
        resp = requests.get(
            ADSBDB_CALLSIGN.format(cs=callsign), headers=HEADERS, timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            origin = (
                data.get("response", {})
                .get("flightroute", {})
                .get("origin", {})
                .get("icao_code")
            )
            if origin:
                return origin.strip().upper()
    except (requests.RequestException, ValueError):
        pass

    return None


def fetch_opensky_states():
    resp = requests.get(OPENSKY_STATES_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("states") or []


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/lon points, in kilometres."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def estimate_eta_text(lat, lon, ground_speed_ms):
    """
    Rough ETA estimate: straight-line distance to the target airport divided
    by current ground speed. This is NOT a real flight-plan ETA - it ignores
    the actual route, remaining descent/holding, and any speed changes - but
    it's a reasonable ballpark for a notification.
    Returns a string like "~1h 42m (approx 14:35 AEST)", or None if we don't
    have enough data (e.g. ground speed is missing or near zero).
    """
    if lat is None or lon is None or not ground_speed_ms or ground_speed_ms < 20:
        return None

    distance_km = haversine_km(lat, lon, TARGET_AIRPORT_LAT, TARGET_AIRPORT_LON)
    speed_kmh = ground_speed_ms * 3.6
    eta_hours = distance_km / speed_kmh

    if eta_hours > 20:
        # Sanity cap - at this range the straight-line estimate is too rough
        # to be worth showing (e.g. still near the departure airport).
        return None

    hours = int(eta_hours)
    minutes = int(round((eta_hours - hours) * 60))
    if minutes == 60:
        hours += 1
        minutes = 0

    arrival_utc = datetime.now(timezone.utc) + timedelta(hours=eta_hours)
    arrival_local = arrival_utc.astimezone(DISPLAY_TZ)
    tz_label = arrival_local.tzname() or "local"

    duration_text = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    return f"~{duration_text} (approx {arrival_local.strftime('%H:%M')} {tz_label})"


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
            origin = get_origin(callsign)
            origin_text = f"from {origin} " if origin else ""
            notes = f" ({entry['notes']})" if entry["notes"] else ""

            lon, lat, ground_speed = state[5], state[6], state[9]
            eta_text = estimate_eta_text(lat, lon, ground_speed)
            eta_part = f" - ETA {eta_text}" if eta_text else ""

            send_ntfy(
                title=f"{reg} inbound to {target_airport}",
                message=(
                    f"{reg}{notes} - {callsign} - {origin_text}"
                    f"heading to {target_airport}{eta_part}"
                ),
            )
            notified[icao24] = now

        time.sleep(0.2)  # be polite to hexdb.io

    save_json(NOTIFIED_FILE, notified)


if __name__ == "__main__":
    main()
