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
  python scripts/refresh_listings.py --kind manual --pages 1 --max-listings 3 --min-delay 0 --max-delay 0
```

8. Check the browser pages:

```text
http://SERVER_IP:8000
http://SERVER_IP:8000/status-page
http://SERVER_IP:8000/refresh-runs-page
http://SERVER_IP:8000/undervalued-page
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

Daily refresh:

```bash
docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --kind daily --pages 50
```

Weekly refresh:

```bash
docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --kind weekly --pages 200
```

Small smoke test:

```bash
docker compose --profile tools run --rm refresh \
  python scripts/refresh_listings.py --kind manual --pages 1 --max-listings 3 --min-delay 0 --max-delay 0
```

Admin endpoint smoke test:

```bash
curl -X POST http://127.0.0.1:8000/refresh-listings \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -d '{"kind":"manual","pages":1,"max_listings":3,"min_delay":0,"max_delay":0}'
```

## Cron Example

Edit crontab:

```bash
crontab -e
```

Example entries:

```cron
# Daily shallow refresh at 03:00.
0 3 * * * cd /opt/krisha && docker compose --profile tools run --rm refresh python scripts/refresh_listings.py --kind daily --pages 50 >> logs/daily-refresh.log 2>&1

# Weekly deeper refresh on Sunday at 04:00.
0 4 * * 0 cd /opt/krisha && docker compose --profile tools run --rm refresh python scripts/refresh_listings.py --kind weekly --pages 200 >> logs/weekly-refresh.log 2>&1
```

Replace `/opt/krisha` with the actual repository path.

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
