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

## Refresh Commands

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
```

Start the website, HTTPS proxy, and bot:

```bash
docker compose --profile https --profile bot up -d --build
```

In the bot, subscribers choose `Астана`, `Алматы`, or `Оба города`. The same
choices are available as `/astana`, `/almaty`, and `/both`.

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
