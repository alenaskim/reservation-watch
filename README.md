# Reservation watch

Watches Resy and OpenTable for open tables at the restaurants you flag and sends a push
notification to your phone (via [ntfy](https://ntfy.sh)) with a link to book. It never
books anything; you tap the alert and book it yourself.

It runs as a small always-on service:

- **Release-time sprints.** For restaurants with a known release schedule (`drop:` in
  `restaurants.yaml`), you get a heads-up 5 minutes before tables drop. From 30 seconds
  before until 10 minutes after, it checks every ~5 seconds and sends an **urgent** alert
  the moment tables appear.
- **Background checks** every 15 minutes across all restaurants and dates, to catch
  cancellations.

## Deploy on Railway (about 10 minutes, ~$5/month)

1. Sign up at [railway.com](https://railway.com) with your GitHub account and pick the
   Hobby plan.
2. **New Project → Deploy from GitHub repo →** `reservation-watch`. Railway finds the
   `Dockerfile` and builds it (the first build takes a few minutes).
3. Open the service → **Variables → New Variable**: `NTFY_TOPIC` = your ntfy topic.
   Railway redeploys automatically.
4. Within a minute, your phone should get **"Reservation watch is running"** with the next
   release time. If it doesn't, check the service's **Deployments → Logs**.

Every push to `main` (e.g. editing `restaurants.yaml` on GitHub) redeploys automatically.
No web domain or open port is needed.

## Phone alerts

Install the **ntfy** app and subscribe to your topic. For release alerts, allow ntfy to
make sound or break through Focus/Do Not Disturb for urgent messages; these alerts are
only useful if you see them within seconds.

## Flagging restaurants

Copy the restaurant's page URL from resy.com or opentable.com into `restaurants.yaml`:

```yaml
- name: Don Angie
  resy: https://resy.com/cities/new-york-ny/venues/don-angie
  next_days: 30
  weekdays: [fri, sat]
  times: "18:30-21:00"
  party_size: 4
```

If the Resy venue lookup fails for a restaurant, add `resy_venue_id: <number>`. You can
find the number in your browser's dev tools (Network tab, look for `venue_id=` on resy.com).

## Test run on GitHub Actions

*Actions → Reservation watch → Run workflow* runs one background check (no sprints).
It needs the `NTFY_TOPIC` repository secret. It has no schedule, so it never duplicates
the server's alerts.

## Run locally

```bash
pip install -r requirements.txt
python -m playwright install chromium   # only needed for OpenTable
python watch.py --dry-run               # one check, prints matches, sends nothing
NTFY_TOPIC=your-topic python watch.py   # one real check
NTFY_TOPIC=your-topic python watch.py --serve   # always-on mode (what Railway runs)
```

## Caveats

- Neither site has a public API. This tool uses the same endpoints as their websites,
  at a low rate. Both sites' terms prohibit automated access, so keep the list short
  and the schedule modest.
- **Resy** is a plain JSON API and is generally reliable.
- **OpenTable** has aggressive bot protection. The script loads the restaurant page in
  headless Chromium and reads the availability data the page fetches. GitHub's servers
  may get blocked; if so, the log says `possibly bot-blocked`. In that case, running
  locally from a home connection usually works.
- Release times in `restaurants.yaml` come from restaurant interviews and guides and can
  change. Torrisi's release time in particular is unconfirmed. If an alert arrives well
  after the heads-up, the time is probably off; adjust `drop:`.
- Even an alert within seconds is a race against booking bots for the hardest tables.
  Have the Resy app open, logged in, with a card saved before the drop.
