# Deploying to a Contabo VPS with Dokploy

Files used: `docker-compose.prod.yml`, `.env.production.example`.

## 0. Before you start
- **Server:** Contabo VPS, Ubuntu 22.04/24.04, at least 8 GB RAM, 4 vCPU, 100 GB disk. The backend image
  bundles Chromium (PDF rendering) and the code executor may use 2 GB on its own.
- **Domains:** two subdomains of ONE domain, both with an A record to the VPS IP, for example
  `app.example.com` (frontend) and `api.example.com` (backend). They must share a registrable domain:
  login cookies are `SameSite=lax`.
- **Code:** Dokploy builds from GitHub, so `docker-compose.prod.yml` must be pushed to the branch you deploy.

## 1. Prepare the server
1. SSH in, update, and create a non-root sudo user if you only have root.
2. Firewall: allow only `22`, `80`, `443` (and `3000` temporarily for Dokploy's first setup, then close it
   and use the Dokploy panel through its own domain). On Contabo also check the panel firewall.
3. Install Dokploy: `curl -sSL https://dokploy.com/install.sh | sh`
4. Open `http://<server-ip>:3000`, create the admin account, then set a domain for the Dokploy panel
   itself (Settings > Server > Domain) so it is served over HTTPS.

## 2. Create the application
1. Create a Project, then add a service of type **Compose**.
2. Source: GitHub, this repository, branch `visual-and-flowchart` (or `main` after merging).
3. Compose type: **Docker Compose**. Compose path: `./docker-compose.prod.yml`.
4. Environment tab: paste `.env.production.example` and fill it in. Generate secrets with
   `openssl rand -hex 48` (JWT) and `openssl rand -hex 24` (database). Copy the AI provider keys from
   your local `.env`. Check that `FRONTEND_ORIGIN` and `NEXT_PUBLIC_API_URL` are the final HTTPS URLs.
5. Domains tab, add two domains with HTTPS and Let's Encrypt:
   - `app.example.com` -> service `frontend`, port `3000`
   - `api.example.com` -> service `backend`, port `8000`
6. Deploy. The first build takes several minutes (Chromium and the Next.js build).

## 3. First-run checks
1. `https://api.example.com/health` returns OK.
2. Backend logs show the Alembic migrations ran, then `starting - ... ai=azure` (not `mock`).
3. Open `https://app.example.com`, register with the `INITIAL_ADMIN_EMAIL` address. That account is the
   admin. Do this right away: until then anyone who registers that email gets admin.
4. Create a short course end to end. Run one code block to confirm the executor works.
5. Open an uploaded course and generate a PDF to confirm Chromium works in the container.

## 4. Production hardening
- **Open registration:** anyone who can reach the site can sign up as an Author and start generations
  that cost AI money. Decide whether to restrict signup (an invite step) or put the site behind
  Cloudflare Access or Traefik basic auth until you have one. Currently not restricted in code.
- **API docs:** FastAPI's `/docs` and `/openapi.json` are public on the API domain by default.
- **Rate limiting:** none in the app. Add it at Traefik/Cloudflare for `/auth/*` and generation routes.
- **Backups:** the data lives in two volumes, `postgres_data` and `backend_data`. Schedule a daily
  `pg_dump` plus a copy of `backend_data` to storage OFF the server (S3, Backblaze, another VPS). Test a
  restore once. Dokploy's backup feature covers databases created as Dokploy database services; it may not
  cover a Postgres defined inside this compose file, so verify before relying on it.
- **Disk space:** old Docker images and build cache grow quickly. Add `docker system prune -f` to a weekly
  cron and an alert at 80% disk.
- **Monitoring:** an uptime check on `/health` for both domains.
- **Updates:** deploy by pushing to the branch; use Dokploy's auto-deploy webhook if you want that.
- **Single worker:** do not raise `--workers` or scale the backend to more than one replica. Job progress
  is held in memory.

## 5. Moving your local data
The production database starts empty. Local courses are not carried over. To migrate them, `pg_dump` the
local database and restore it into the server's Postgres, and copy `backend/data` into the `backend_data`
volume. Only do this for data you want in production; it includes the local test users.

## 6. Troubleshooting
- **Login works but you are logged out on reload:** the two domains are not under one registrable domain,
  or `FRONTEND_ORIGIN` does not match the browser address exactly (scheme, host, no trailing slash).
- **Browser calls go to localhost:8000:** `NEXT_PUBLIC_API_URL` was empty or wrong at build time. Fix it
  and redeploy with a rebuild.
- **502 from Traefik:** the domain points at the wrong service or port, or the container is not on
  `dokploy-network`.
- **Backend exits at start:** read its log. A missing required variable is reported by name.
