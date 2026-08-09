#!/usr/bin/env python3
"""
YSSY Special Livery Arrival Watcher
------------------------------------
Reads a list of tracked aircraft registrations from a Google Sheet,
checks their live positions via OpenSky, and sends a push
notification (via ntfy.sh) the first time a tracked aircraft is
found (while airborne) heading TO YSSY.

This is the ARRIVALS-only half of the project - the pre-departure
"assigned a new flight number while parked" detection lives in a
separate repo/script now, to keep each run fast and simple rather
than doing both jobs' worth of lookups every 5 minutes.

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

# Optional - if set, real (live-tracked) ETAs from AeroDataBox are used
# instead of the physics-based estimate whenever available. Get a free key
# at rapidapi.com/aedbx-aedbx/api/aerodatabox (Basic/free tier).
AERODATABOX_API_KEY = os.environ.get("AERODATABOX_API_KEY", "")

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

    try:
        resp = requests.get(
            ADSBDB_CALLSIGN.format(cs=callsign), headers=HEADERS, timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            dest = (
                data.get("response", {})
                .get("flightroute", {})
                .get("destination", {})
                .get("icao_code")
            )
            if dest:
                return dest.strip().upper()
    except (requests.RequestException, ValueError):
        pass

    return None


def get_origin(callsign):
    """Resolve a callsign to its departure airport ICAO code, if known."""
    callsign = callsign.strip()
    if not callsign:
        return None

    url = HEXDB_CALLSIGN_ORIGIN.format(cs=callsign)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            origin = resp.text.strip().upper()
            if origin and "not found" not in origin.lower() and len(origin) == 4:
                return origin
    except requests.RequestException:
        pass

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


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing from point 1 to point 2, in degrees (0-360)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def heading_is_plausible(lat, lon, true_track, target_lat, target_lon, max_deviation_deg=75):
    """
    Sanity check: does the aircraft's actual reported heading roughly agree
    with the bearing it would need to fly to reach the target airport?
    Guards against a stale/wrong destination lookup (more common for
    charter/cargo carriers that reuse callsigns across different ad-hoc
    routes). Fails open (returns True) if position/track data is missing.
    """
    if lat is None or lon is None or true_track is None:
        return True

    required_bearing = bearing_deg(lat, lon, target_lat, target_lon)
    diff = abs(true_track - required_bearing) % 360
    if diff > 180:
        diff = 360 - diff

    return diff <= max_deviation_deg


# If the aircraft's current ground speed is below this, it's likely still
# climbing out rather than at cruise - using that slow instantaneous speed
# to project the ENTIRE remaining distance badly overestimates flight time
# (this is what caused ETAs to come out hours too late for aircraft caught
# shortly after takeoff on a long route).
CRUISE_SPEED_FLOOR_MS = 180  # ~350 kt

# Reasonable average ground speed to assume instead, once we've decided the
# current reading isn't representative of the rest of the flight.
ASSUMED_CRUISE_SPEED_MS = 230  # ~447 kt / ~828 km/h


def estimate_eta_text(lat, lon, ground_speed_ms):
    """
    Rough ETA estimate: straight-line distance to the target airport divided
    by a representative ground speed. NOT a real flight-plan ETA - ignores
    actual routing, altitude changes, wind, and holding.
    """
    if lat is None or lon is None or not ground_speed_ms or ground_speed_ms < 20:
        return None

    distance_km = haversine_km(lat, lon, TARGET_AIRPORT_LAT, TARGET_AIRPORT_LON)

    speed_for_eta_ms = ground_speed_ms
    still_climbing_note = ""
    if ground_speed_ms < CRUISE_SPEED_FLOOR_MS and distance_km > 300:
        # Current speed looks like climb-out, not cruise, and there's a lot
        # of distance left - substitute an assumed cruise speed rather than
        # projecting the slow current speed across the whole remaining trip.
        speed_for_eta_ms = ASSUMED_CRUISE_SPEED_MS
        still_climbing_note = ", still climbing - rough estimate"

    speed_kmh = speed_for_eta_ms * 3.6
    eta_hours = distance_km / speed_kmh

    if eta_hours > 20:
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
    return f"~{duration_text} (approx {arrival_local.strftime('%H:%M')} {tz_label}{still_climbing_note})"


def get_real_eta(registration, target_airport):
    """
    Look up the REAL, live-tracked arrival time from AeroDataBox, rather
    than estimating from current speed/distance. Only called for an
    aircraft that's already been matched via live OpenSky data as heading
    to the target airport - this is a refinement of an existing match,
    not a replacement for the detection itself.

    Returns a formatted string like "09:06 local (live-tracked, flight
    QF 589)", or None if AeroDataBox isn't configured, has no data for
    this flight, or the request fails for any reason (fails silently so
    the caller can fall back to the physics-based estimate).
    """
    if not AERODATABOX_API_KEY:
        return None

    url = f"https://aerodatabox.p.rapidapi.com/flights/reg/{registration}"
    params = {"withAircraftImage": "false", "withLocation": "false", "withFlightPlan": "false"}
    headers = {
        "x-rapidapi-host": "aerodatabox.p.rapidapi.com",
        "x-rapidapi-key": AERODATABOX_API_KEY,
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            return None
        flights = resp.json()
        if not isinstance(flights, list) or not flights:
            return None
    except (requests.RequestException, ValueError):
        return None

    candidates = [
        f for f in flights
        if f.get("arrival", {}).get("airport", {}).get("icao") == target_airport
    ]
    if not candidates:
        return None

    # Prefer the actual operating flight (not a codeshare number) that's
    # currently en route over one that's merely "Expected" later today.
    candidates.sort(key=lambda f: (
        f.get("codeshareStatus") != "IsOperator",
        f.get("status") != "EnRoute",
    ))
    flight = candidates[0]
    arrival = flight.get("arrival", {})

    predicted = arrival.get("predictedTime", {}).get("local")
    revised = arrival.get("revisedTime", {}).get("local")
    scheduled = arrival.get("scheduledTime", {}).get("local")
    best_time = predicted or revised or scheduled
    if not best_time:
        return None

    source = "live-tracked" if predicted else ("revised" if revised else "scheduled")
    time_part = best_time.split(" ")[1][:5] if " " in best_time else best_time
    flight_number = flight.get("number", "")

    return f"{time_part} local ({source}, flight {flight_number})"


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

    tracked = {}
    for entry in registrations:
        reg = entry["registration"]
        hexcode = get_icao24(reg, icao24_cache)
        if hexcode:
            tracked[hexcode] = entry
        else:
            print(f"Could not resolve ICAO24 for registration {reg}")
        time.sleep(0.2)

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
            continue  # arrivals-only: skip parked aircraft entirely

        entry = tracked[icao24]
        reg = entry["registration"]
        notes = f" ({entry['notes']})" if entry["notes"] else ""

        last_notified = notified.get(icao24)
        if last_notified and (now - last_notified) < RENOTIFY_AFTER_HOURS * 3600:
            continue

        dest = get_destination(callsign)
        print(f"{reg} ({callsign}) -> destination {dest!r} (comparing to target {target_airport!r})")

        if dest == target_airport:
            lon, lat, ground_speed = state[5], state[6], state[9]
            true_track = state[10]

            if not heading_is_plausible(lat, lon, true_track, target_lat=TARGET_AIRPORT_LAT, target_lon=TARGET_AIRPORT_LON):
                required_bearing = bearing_deg(lat, lon, TARGET_AIRPORT_LAT, TARGET_AIRPORT_LON)
                print(
                    f"  -> SUPPRESSED: {reg} destination lookup says {target_airport}, "
                    f"but actual track ({true_track}) doesn't point that way "
                    f"(would need ~{required_bearing:.0f}). Likely a stale route "
                    f"lookup (common for charter/cargo callsigns) - not alerting."
                )
            else:
                origin = get_origin(callsign)
                origin_text = f"from {origin} " if origin else ""

                real_eta = get_real_eta(reg, target_airport)
                if real_eta:
                    eta_part = f" - ETA {real_eta}"
                else:
                    eta_text = estimate_eta_text(lat, lon, ground_speed)
                    eta_part = f" - ETA {eta_text} (estimated)" if eta_text else ""

                send_ntfy(
                    title=f"{reg} inbound to {target_airport}",
                    message=(
                        f"{reg}{notes} - {callsign} - {origin_text}"
                        f"heading to {target_airport}{eta_part}"
                    ),
                )
                notified[icao24] = now

        time.sleep(0.2)

    save_json(NOTIFIED_FILE, notified)


if __name__ == "__main__":
    main()
