# YSSY Special Livery Watcher — Setup Guide

No coding required. This will check every 5 minutes whether any aircraft on
your list is flying to Sydney (YSSY), and push a notification to your phone
when one is.

**This is a clean rebuild of the project with all fixes included** — follow
this guide fresh in a new repository rather than reusing an old one, to
avoid carrying over any partial/inconsistent edits.

## 1. Create your aircraft database (Google Sheet)

1. Go to [sheets.google.com](https://sheets.google.com) and create a new blank sheet.
2. In row 1, add two column headers: `registration` and `notes`.
3. From row 2 down, add one aircraft per row, e.g.:

   | registration | notes                     |
   |--------------|---------------------------|
   | VH-OQA       | Qantas "Wunala Dreaming"  |
   | VH-XXX       | Special retro livery      |

4. Click **File > Share > Publish to web**.
5. Under "Link", change the dropdown from "Web page" to **CSV**, then click **Publish**.
6. Copy that URL — you'll need it in step 3.

Whenever you want to add or remove a tracked aircraft, just edit rows in
this same sheet. No republishing needed — the published CSV link always
reflects the sheet's current contents.

## 2. Set up your phone notifications (ntfy)

1. Install the free **ntfy** app from the Play Store.
2. Open it, tap **+ Subscribe to topic**.
3. Type a topic name only you would guess (e.g. `rob-yssy-livery-7k2`) —
   anyone who knows the exact name could see your notifications, so make it
   unique, not something guessable like "yssy-watcher".
4. That's it — leave the app installed. Notifications will arrive here.

## 3. Create the GitHub repository (this is the free "always-on" part)

1. Go to [github.com](https://github.com) and create a free account if you
   don't have one.
2. Click **+ > New repository**. Name it anything (e.g. `yssy-watcher-v2`).
   Set it to **Private**. Click **Create repository**.
3. On the new repo page, click **Add file > Upload files**, and drag in
   every file from this project **as a single batch**: `watcher.py`,
   `requirements.txt`, `SETUP.md`, and the whole `.github` folder (with
   `workflows/watch.yml` inside it). Commit the upload.
4. **Verify the folder structure landed correctly** — this is the most
   common thing to go wrong. Go to the repo's **Code** tab and confirm you
   see a `.github` folder at the top level; click into it, then into
   `workflows`, and confirm `watch.yml` is there (not sitting loose at the
   repo's top level). If it's in the wrong place, delete it and instead use
   **Add file > Create new file**, typing the full path
   `.github/workflows/watch.yml` in the filename box (GitHub auto-creates
   the folders when you type `/`), then paste the content in and commit.
5. Go to the repo's **Settings > Secrets and variables > Actions**.
6. Click **New repository secret** and add:
   - Name: `GOOGLE_SHEET_CSV_URL` → Value: the CSV link from step 1
   - Name: `NTFY_TOPIC` → Value: the topic name from step 2
7. Go to the **Actions** tab of your repo, click on "YSSY Livery Watcher",
   and click **Run workflow** to test it manually first.
8. Open that run's **"Run watcher"** step and confirm the very first line
   of output reads `Target airport is: 'YSSY' (length 4)` — if it shows
   anything else (extra characters, wrong length), that's caught
   immediately rather than silently causing missed notifications later.

That's the whole setup. From here it runs itself every 5 minutes for free,
forever, with zero maintenance — just edit the Google Sheet whenever you
want to track a different aircraft.

## How it actually detects "flying to YSSY"

For each aircraft on your list, the script:
1. Looks up its live position via OpenSky's free ADS-B data feed.
2. If it's airborne, reads its current flight callsign.
3. Looks up that callsign's filed destination airport.
4. If the destination is YSSY, sends you a notification (once per ~20 hours
   per aircraft, so it won't spam you repeatedly for the same flight).

## Known limitations (please read)

- **Destination lookup depends on public flight-schedule data** (via
  hexdb.io). It's usually reliable for scheduled airline flights, but can
  miss unscheduled charters, ferry flights, or freshly-changed callsigns.
- **OpenSky's free/anonymous tier** updates roughly every 10 seconds when
  queried, but has a daily request cap — checking every 5 minutes stays
  comfortably within it.
- This won't give you a photography answer on its own (framing, timing,
  weather) — just an early heads-up that a tracked airframe is inbound.
- If you ever want tighter accuracy (e.g. also alerting on descent rate or
  distance-to-airport, not just filed destination), that's a small addition
  to `watcher.py` — just ask.
