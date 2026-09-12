# Deploying to a VPS

Docker Compose (app + Caddy for automatic HTTPS), with persistent
volumes so job history and generated files survive redeploys. This is
the recommended path — it reuses the `Dockerfile` from the professional
hardening pass (non-root user, healthcheck, all the `libgl1`/OCCT deps
already worked out) rather than fighting with a native CadQuery install
directly on the host.

**What you need before starting:**
- A VPS (2GB RAM minimum — CadQuery/OCCT is not lightweight; 4GB is more
  comfortable if you'll run more than one generation concurrently)
- Ubuntu 22.04 or 24.04 (these instructions assume that; Debian works
  near-identically)
- A domain name pointed at the VPS's IP (an A record), if you want real
  HTTPS. You can skip this and run on plain HTTP for initial testing —
  see the Caddyfile's commented-out block.

---

## 1. Bootstrap the VPS (one time)

```bash
scp deploy/setup-vps.sh root@YOUR_VPS_IP:~/
ssh root@YOUR_VPS_IP
bash setup-vps.sh
```

This installs Docker, creates a non-root `nitocad` deploy user, and
locks the firewall down to SSH/HTTP/HTTPS only (UFW + fail2ban). Read
the script before running it — it's short and does exactly what its
comments say, nothing hidden.

Log out and back in as the new user afterward (needed for the `docker`
group membership to apply):

```bash
ssh nitocad@YOUR_VPS_IP
```

## 2. Get the code onto the VPS

Either `git clone` your repo (recommended — makes `deploy/deploy.sh`'s
`git pull` step work for future updates), or `scp`/`rsync` this
directory over directly:

```bash
# option A: git
git clone <your-repo-url> nitocad-pro && cd nitocad-pro

# option B: copy from your machine
rsync -avz --exclude output --exclude .env ./nitocad_pro/ nitocad@YOUR_VPS_IP:~/nitocad-pro/
```

## 3. Configure

```bash
cd nitocad-pro
cp .env.example .env
nano .env   # or vim/your editor of choice
```

At minimum for a real deploy, set:
- `ENVIRONMENT=production`
- `CORS_ORIGINS=https://your-frontend-domain.com` (not `*` — see the
  comment in `.env.example`)
- `DEEPSEEK_API_KEY` if you want server-side DeepSeek parsing available
  without every caller bringing their own key
- R2 credentials if you want generated files to survive beyond this
  one VPS's disk (recommended, but not required to get started — the
  `nitocad-data` volume covers you for a single-VPS setup either way)

Then edit `Caddyfile` and replace `your-domain.com` with your real
domain. If you don't have a domain yet, comment out the domain block and
uncomment the `:80` block instead — switch back before going live.

## 4. First deploy

```bash
docker compose up -d --build
```

First build takes a few minutes (installing CadQuery's OCP wheel isn't
fast). Watch it come up:

```bash
docker compose logs -f app
```

You're looking for `starting nitocad api` in the logs and no
`ImportError` around `cadquery`/`libGL`. Once it's up:

```bash
curl http://localhost/healthz   # or https://your-domain.com/healthz once DNS/Caddy are live
```

Expect `{"status":"ok","version":"2.0.0","environment":"production"}`.

If you configured a real domain, Caddy requests its Let's Encrypt
certificate automatically on first request — give it a minute the very
first time.

## 5. Verify end to end

```bash
# Issue a key
curl -X POST https://your-domain.com/api/keys/generate

# Generate a part
curl -X POST https://your-domain.com/generate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key from above>" \
  -d '{"description": "shaft 10mm diameter, 50mm long"}'
```

This is also the point to run the test suite for real, per the
Changelog's note that it was written but not run against a live
CadQuery install:

```bash
docker compose exec app pip install pytest pytest-cov httpx
docker compose exec app pytest
```

## 6. Ongoing deploys

```bash
./deploy/deploy.sh
```

Pulls latest code, rebuilds the `app` image, restarts, and waits for
`/healthz` before finishing. Caddy keeps running throughout (only `app`
restarts), so there's a brief window of 502s during the swap — fine for
a solo VPS deploy, not a rolling/zero-downtime deploy.

For automatic deploys on push to `main`, see
`.github/workflows/deploy.yml` — disabled until you add the four
`VPS_*` secrets it documents; manual `./deploy/deploy.sh` works fine
without it.

## 7. Backups

```bash
./deploy/backup.sh
```

Backs up the sqlite DB (job history, API keys) via `sqlite3`'s own
`.backup()` — safe to run against a live database, unlike copying the
file directly. Wire it into cron for daily backups (the script's own
header comment has the exact crontab line). If you've configured R2,
your generated STEP/STL files are already durable independent of this;
if not, they only live in the `nitocad-data` volume and this backup
script doesn't currently cover them (only the DB) — extend it or turn
on R2 if that matters to you.

## 8. Monitoring day to day

```bash
docker compose ps                    # is everything up?
docker compose logs -f app           # structured JSON logs (LOG_FORMAT=json in compose)
docker compose logs -f caddy         # reverse proxy / TLS logs
curl https://your-domain.com/readyz  # is the DB actually reachable, not just "process alive"
```

`LOG_FORMAT=json` (set in `docker-compose.yml`) means every line is a
parseable JSON object with `request_id`, `level`, `duration_ms`, etc. —
pipe `docker compose logs app` into `jq` for quick filtering:

```bash
docker compose logs app --no-log-prefix | jq 'select(.level=="ERROR")'
```

## Troubleshooting

- **`ImportError: libGL.so.1`** — you're running the app outside Docker
  (bare `pip install` on the VPS directly) without the OpenGL/X11 libs
  the Dockerfile installs. Either use Docker (recommended) or install
  `libgl1 libglib2.0-0 libxrender1 libsm6 libxext6` on the host yourself.
- **502 from Caddy right after deploy** — normal for a few seconds while
  `app` restarts; `deploy.sh` waits for `/healthz` before declaring
  success, so if it exits cleanly the deploy worked even if you saw a
  transient 502 mid-deploy.
- **Cert not issuing** — confirm the domain's A record actually points
  at this VPS (`dig your-domain.com`) and that port 80 is reachable from
  the internet (Let's Encrypt's HTTP-01 challenge needs it, even though
  you're serving on 443 afterward) — check `ufw status` and your cloud
  provider's own firewall/security-group rules, which UFW doesn't
  control.
- **Permission errors writing to `/data`** — should be handled by the
  Dockerfile's `chown` step, but if you changed the volume setup, see
  that Dockerfile section's comment for why the ownership matters on
  first volume initialization.

## What's deliberately not covered here

- Multi-instance/load-balanced deploys (this whole setup assumes one
  VPS, one `app` container — sqlite doesn't handle concurrent writers
  from multiple instances well; you'd want Postgres first)
- Kubernetes / anything beyond Compose — not warranted at this scale
- Log aggregation beyond `docker compose logs` — ship the JSON logs to
  something like Loki/Datadog/CloudWatch once volume justifies it; the
  structured format (`logging_config.py`) is already the hard part done
