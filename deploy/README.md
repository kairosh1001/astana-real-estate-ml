# VPS Deployment Notes

This is the intended low-cost v1 deployment path:

1. Rent a small VPS, such as Hetzner CX23 if pricing is acceptable.
2. Install Docker and the Docker Compose plugin.
3. Clone this repository onto the server.
4. Copy `.env.example` to `.env` and adjust values if needed.
5. Start the app with Docker Compose.
6. Add cron jobs for daily and weekly refreshes.
7. Add Caddy/HTTPS after the app works by server IP.

## VPS Checklist

Use this order when deploying for the first time:

1. Buy the VPS and SSH into it.
2. Install Docker and the Docker Compose plugin.
3. Clone the repo:

```bash
git clone https://github.com/kairosh1001/astana-real-estate-ml.git /opt/krisha
cd /opt/krisha
```

4. Create environment config:

```bash
cp .env.example .env
nano .env
```

Set at least:

```text
ADMIN_TOKEN=replace-with-a-private-token
APP_PORT=8000
DB_PATH=/app/data/krisha.sqlite3
```

5. Build and start:

```bash
docker compose build
docker compose up -d app
docker compose ps
```

6. Verify the app:

```bash
curl http://127.0.0.1:8000/health
docker compose run --rm app python scripts/check_deployment.py
docker compose run --rm app python scripts/check_ui.py
```

7. Run a tiny scrape smoke test:

```bash
docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --city almaty --kind manual --pages 1 --max-listings 3 --min-delay 0 --max-delay 0
```

8. Check the browser pages:

```text
http://SERVER_IP:8000
http://SERVER_IP:8000/status-page
http://SERVER_IP:8000/refresh-runs-page
http://SERVER_IP:8000/undervalued-page
http://SERVER_IP:8000/undervalued-page?city=almaty
```

9. Add cron only after the smoke test succeeds.
10. Add a domain and HTTPS only after the IP-based app works.

## First Server Run

From the repository directory:

```bash
cp .env.example .env
docker compose build
docker compose up -d app
docker compose ps
curl http://127.0.0.1:8000/health
```

Before exposing the app publicly, edit `.env` and replace `ADMIN_TOKEN=change-me`
with a private value.

If the server firewall allows port 8000, the app can be tested by IP first:

```text
http://SERVER_IP:8000
```

## Updating an Existing VPS for Phase 3

Phase 3 adds account, session, and saved-listing tables. They are created
automatically and idempotently when the new app container starts; no manual SQL
migration is required. Back up SQLite before the first Phase 3 start because the
database now also contains account emails, saved URLs, and personal notes.

First push the Phase 3 commit from the development machine:

```bash
git push origin main
```

Then connect to the VPS and run:

```bash
cd /opt/krisha
git pull --ff-only origin main

docker compose run --rm app \
  python scripts/backup_db.py \
  --db /app/data/krisha.sqlite3 \
  --out-dir /app/backups
```

Keep the existing secrets in `.env`. For the public HTTPS deployment, add or
update these values:

```text
APP_DOMAIN=kvartiry-ai.kz
COOKIE_SECURE=true
```

Rebuild both the application and the scheduled refresh worker, then recreate the
application behind Caddy. They have separate images; rebuilding only `app` does
not update the `refresh` image used by cron:

```bash
docker compose --profile tools build app refresh
docker compose --profile https up -d app caddy
docker compose ps
docker compose logs --tail=100 app
```

Verify the new container and public endpoint:

```bash
curl -fsS https://kvartiry-ai.kz/health
docker compose exec -T app python scripts/check_deployment.py
docker compose exec -T app python scripts/check_ui.py
```

Finally, open these pages in a private browser window and test one account save:

```text
https://kvartiry-ai.kz/register
https://kvartiry-ai.kz/login
https://kvartiry-ai.kz/saved-listings
```

If the VPS repository lives somewhere other than `/opt/krisha`, use its actual
path. `COOKIE_SECURE=true` means account login must be tested through HTTPS, not
through a direct `http://SERVER_IP:8000` URL.

## Refresh Commands

### Diagnosing a failed daily refresh

For a run reporting zero processed listings, inspect the actual failure before
starting another full scan:

```bash
tail -n 80 logs/daily-astana-refresh.log
```

After pulling an update, rebuild the worker explicitly and run a bounded smoke
test in a separate diagnostic database:

```bash
git pull --ff-only origin main
docker compose --profile tools build app refresh
docker compose up -d app
flock -w 60 /tmp/krisha-refresh.lock docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --city astana --kind manual \
  --pages 1 --max-listings 3 --db /app/data/refresh-smoke.sqlite3
```

Proceed with the normal daily command only after the smoke test reports
`status='completed'`, three processed listings, and zero errors. If the Telegram
bot is enabled, rebuild/recreate it separately with
`docker compose --profile bot up -d --build telegram_bot`.

Refresh logs and the stored run error now distinguish `parse`, `predict`, and
`store` failures. A run with no processed listings is failed, not completed;
partial/failed runs return exit code 1. The worker stops after 10 consecutive
listing failures (configurable with `--max-consecutive-listing-failures`) or an
HTTP 401/403/429/468 denial, and preserves request pacing on failures. Do not repeatedly
rerun an access-denied job. Failed/partial weekly scans do not age unseen listings.
`--max-listings` limits attempts, including failed ones, so a smoke test stays small.

HTTP 468 was observed on listing pages from the VPS while category pages still
returned URLs. It is a nonstandard upstream rejection, not a model exception.
The status alone does not identify the security vendor, whether the server IP
is blocked, or how long the restriction lasts. Keep full scans paused while
access is rejected and contact Krisha about permitted automated access (include
the VPS public IP, time, example URL, and response status). Changing this site's
Cloudflare settings does not change outgoing requests from the worker to Krisha.
Stopping safely does not itself restore upstream access.

To compare one recently checked active listing per city from the VPS, run:

```bash
docker compose --profile tools run --rm refresh python scripts/diagnose_city_access.py
```

This opens the listing database read-only, uses one scraper session with retries
disabled, and sends only two listing requests spaced two seconds apart. It does
not follow redirects or fetch developers, run models, or write listing data.
The JSON includes UTC timestamps, HTTP status, selected response headers and
fixed page-protection markers, never cookies or raw response bodies. Run it on
the VPS: results from a development machine do not establish VPS access.
One successful/rejected pair is evidence about those requests, not proof of a
city-wide restriction.

If a saved listing returns 200 but a fresh refresh returns 468, compare the exact
failed URL, not another saved listing:

```bash
docker compose --profile tools run --rm refresh python scripts/diagnose_city_access.py \
  --city astana --compare-url https://krisha.kz/a/show/1013552697
```

Replace the example URL with the failed URL. This mode does not open the database.
It requests that listing, the city category, then the same listing in one unchanged
session/User-Agent. It stops on the first non-200 response or missing listing
markup, with at most three requests. The report includes the selected User-Agent
because the existing scraper chooses one randomly at startup; separate diagnostic
and refresh processes can therefore differ. A changed response after visiting the
category suggests sequence/session dependence, but does not prove a cookie-specific
cause or rule out time-dependent protection. Do not use repeated runs or identity
rotation to try to obtain an accepted response.

After the first deployment that includes the rental model, backfill estimates for
sale listings already stored in the database:

```bash
docker compose --profile tools run --rm refresh \
  python scripts/backfill_rental_estimates.py
```

Rental observations are stored separately from sale listings. Refresh the current
monthly-rent inventory with:

```bash
docker compose --profile tools run --rm refresh \
  python scripts/refresh_rentals.py --city astana --pages 100
docker compose --profile tools run --rm refresh \
  python scripts/refresh_rentals.py --city almaty --pages 100
```

Daily refresh for both cities:

```bash
docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --city astana --kind daily --pages 100
docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --city almaty --kind daily --pages 100
```

Weekly refresh for both cities:

```bash
docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --city astana --kind weekly --pages 200
docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --city almaty --kind weekly --pages 200
```

Small smoke test:

```bash
docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --city almaty --kind manual --pages 1 --max-listings 3 --min-delay 0 --max-delay 0
```

One-time Almaty recovery/backfill after deploying city support:

```bash
docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --city almaty --kind manual --pages 200
```

Watch the final line in the command output. `pages`, `urls`, `processed`, and
`failed` show whether Krisha pages were actually parsed; the page limit alone does
not guarantee that every response contained listings.

Admin endpoint smoke test:

```bash
curl -X POST http://127.0.0.1:8000/refresh-listings \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -d '{"city":"almaty","kind":"manual","pages":1,"max_listings":3,"min_delay":0,"max_delay":0}'
```

## Cron Example

Create the log directory once before installing the jobs:

```bash
mkdir -p /opt/krisha/logs
```

Edit crontab:

```bash
crontab -e
```

Example entries:

```cron
# Daily shallow refreshes for Astana and Almaty.
0 3 * * * cd /opt/krisha && flock -w 21600 /tmp/krisha-refresh.lock docker compose --profile tools run --rm refresh python scripts/refresh_listings.py --city astana --kind daily --pages 100 >> logs/daily-astana-refresh.log 2>&1
0 7 * * * cd /opt/krisha && flock -w 21600 /tmp/krisha-refresh.lock docker compose --profile tools run --rm refresh python scripts/refresh_listings.py --city almaty --kind daily --pages 100 >> logs/daily-almaty-refresh.log 2>&1

# Weekly deeper refreshes, separated by day.
0 15 * * 0 cd /opt/krisha && flock -w 21600 /tmp/krisha-refresh.lock docker compose --profile tools run --rm refresh python scripts/refresh_listings.py --city astana --kind weekly --pages 200 >> logs/weekly-astana-refresh.log 2>&1
0 15 * * 6 cd /opt/krisha && flock -w 21600 /tmp/krisha-refresh.lock docker compose --profile tools run --rm refresh python scripts/refresh_listings.py --city almaty --kind weekly --pages 200 >> logs/weekly-almaty-refresh.log 2>&1

# Daily monthly-rent inventory used by the investment estimate.
30 11 * * * cd /opt/krisha && flock -w 21600 /tmp/krisha-refresh.lock docker compose --profile tools run --rm refresh python scripts/refresh_rentals.py --city astana --pages 100 >> logs/daily-rent-astana-refresh.log 2>&1
30 19 * * * cd /opt/krisha && flock -w 21600 /tmp/krisha-refresh.lock docker compose --profile tools run --rm refresh python scripts/refresh_rentals.py --city almaty --pages 100 >> logs/daily-rent-almaty-refresh.log 2>&1
```

Replace `/opt/krisha` with the actual repository path.
Both jobs use the same lock, so a slow refresh waits instead of running concurrently.
Interrupted refresh records older than 18 hours are closed automatically on the next run.

## Caddy/HTTPS

Use the `https` Compose profile only after a real domain points to the VPS.
The provider panel hostname is not enough by itself; you must own the domain and
create a DNS `A` record pointing to the server IP.

Without a domain, keep testing by IP and port first.

DNS example:

```text
your-domain.kz  A  SERVER_IP
```

After DNS is ready, edit `.env`:

```text
APP_DOMAIN=your-domain.kz
```

Then start Caddy:

```bash
docker compose --profile https up -d app caddy
docker compose ps
docker compose logs --tail=100 caddy
```

Expected public URLs:

```text
https://your-domain.kz
https://your-domain.kz/undervalued-page
https://your-domain.kz/status-page
```

If `www.your-domain.kz` also has an `A` record pointing to the VPS, Caddy
redirects it to the main domain without `www`.

Internal pages still redirect to `/admin-login` and use `ADMIN_TOKEN` as the
password.

After HTTPS works, optionally bind the app port to localhost in `.env` so direct
public access to `:8000` is no longer exposed:

```text
APP_PORT=127.0.0.1:8000
```

Then restart:

```bash
docker compose --profile https up -d app caddy
```

## Telegram Bot

Create a bot in Telegram via `@BotFather`. It will give you:

- `TELEGRAM_BOT_TOKEN`, a secret token;
- `TELEGRAM_BOT_USERNAME`, the bot username without `@`.

Add them to `.env`:

```text
APP_PUBLIC_URL=https://your-domain.kz
TELEGRAM_BOT_TOKEN=123456:bot-token-from-botfather
TELEGRAM_BOT_USERNAME=your_bot_username
TELEGRAM_DIGEST_HOUR_ASTANA=9
TELEGRAM_ADMIN_PASSWORD=use-a-separate-long-random-password
TELEGRAM_ADMIN_REPORT_HOUR_ASTANA=8
```

Start the website, HTTPS proxy, and bot:

```bash
docker compose --profile https --profile bot up -d --build
```

In the bot, subscribers choose `Астана`, `Алматы`, or `Оба города`. The same
choices are available as `/astana`, `/almaty`, and `/both`.

To connect a private admin report, open a direct chat with the bot and send:

```text
/admin use-a-separate-long-random-password
```

Do not send this command in a group. The bot tries to delete the password message
immediately and stores only the approved Telegram chat ID. Admin commands:

- `/admin_status` sends a report immediately;
- `/admin_off` pauses daily reports without revoking access;
- `/admin_on` resumes daily reports;
- `/admin_logout` removes the approved chat.

At `TELEGRAM_ADMIN_REPORT_HOUR_ASTANA`, the bot reports the public health check,
24-hour traffic, 5xx/429 counts, aggregate registrations and saves, listing totals
for Astana and Almaty, and the most recent refresh result. Personal user data is
not sent to Telegram. Use at least 16 characters and keep
`TELEGRAM_ADMIN_PASSWORD` different from `ADMIN_TOKEN`.

Check logs:

```bash
docker compose logs --tail=100 telegram_bot
```

If you do not want the bot running, omit `--profile bot`.

## Runtime Data

SQLite data is stored in:

```text
./data/krisha.sqlite3
```

Back this file up before rebuilding or moving servers. The Compose file mounts `./data` into the app container, so normal container rebuilds should not delete it.

Manual backup:

```bash
python scripts/backup_db.py
```

Docker backup:

```bash
docker compose run --rm app python scripts/backup_db.py
```

On Windows local development, use the host Python command instead. Dockerized backups are intended for the Linux VPS deployment.

Backups are written to:

```text
./backups/
```

The script keeps the newest 14 backups by default. Use `--keep 0` to keep all.
