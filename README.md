# Reservation watch

Checks Resy and OpenTable for open tables at the restaurants you flag, and sends a push
notification to your phone (via [ntfy](https://ntfy.sh)) with a link to book. It runs
for free on a schedule with GitHub Actions. It never books anything; you tap the alert
and book it yourself.

## Setup (about 10 minutes)

1. **Phone alerts:** install the **ntfy** app (iOS/Android) and subscribe to a topic
   with a long, unguessable name, e.g. `tables-k3v9x2mq7`. Anyone who knows the topic
   name can read it, so treat the name like a password.
2. **Repo:** push this folder to a new GitHub repo.
3. **Secret:** in the repo, go to *Settings → Secrets and variables → Actions →
   New repository secret*. Name it `NTFY_TOPIC` and set it to your topic name.
4. **Restaurants:** edit `restaurants.yaml` (see the comments there) and commit.
5. **Test:** go to *Actions → Reservation watch → Run workflow*. Check the log; you
   should see a line per restaurant like `Lilia: 3 slot(s) in window…`.

After that it runs every 15 minutes. You get one alert per newly opened slot. If a slot
gets taken and opens up again later, you get alerted again.

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

## Private vs public repo

- **Public repo:** unlimited free Action minutes. Your restaurant list is visible, but
  your ntfy topic stays secret.
- **Private repo:** 2,000 free minutes/month, and every run bills at least 1 minute.
  Running every 15 minutes uses about 2,900/month, so change the cron in
  `.github/workflows/watch.yml` to `"*/30 * * * *"`, or limit it to waking hours,
  e.g. `"*/15 12-23,0-4 * * *"` (UTC).

## Run locally

```bash
pip install -r requirements.txt
python -m playwright install chromium   # only needed for OpenTable
python watch.py --dry-run               # prints matches, sends nothing
NTFY_TOPIC=your-topic python watch.py   # real run
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
- Many hot restaurants release tables at a fixed time (e.g. 10:00 AM, 30 days out).
  A check every 15 minutes catches cancellations well but won't beat the release rush.
