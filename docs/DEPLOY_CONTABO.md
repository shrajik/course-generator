# Deploying to a Contabo VPS with GitHub Actions (no Dokploy)

`deploy/deploy.py` (standard-library Python) uploads the code over SSH and runs `docker compose` on the
server. You can run it from your PC, or let `.github/workflows/deploy.yml` run it on every push to
`Feedback-work`. Files: `deploy/deploy.py`, `docker-compose.prod.yml`, `.env.production.example`,
`deploy/Caddyfile` (only for the optional bundled proxy).

```
python deploy/deploy.py --ssh--> Contabo server (compose project "coursegen")
                                  |- frontend, backend  --web network-->  the host's Caddy (HTTPS)
                                  |- backend --> postgres, code-executor (isolated)
```

By default the stack publishes **no host ports**. A reverse proxy that already runs on the server (for
example the Caddy that serves another app there) reaches the app on the shared Docker network `web` as
`coursegen-frontend:3000` and `coursegen-backend:8000`. Section 5 covers that. Use `--with-caddy` only on a
server that has no proxy yet.

## 1. One-time server setup (as root over SSH)
Ubuntu 22.04/24.04 assumed. Check with `cat /etc/os-release`.

```bash
# updates, tools, Docker
apt update && apt -y upgrade
apt -y install rsync ufw curl
curl -fsSL https://get.docker.com | sh

# a dedicated deploy user in the docker group (docker group access is effectively root: keep this user
# for deployments only)
adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
mkdir -p /opt/course-generator && chown deploy:deploy /opt/course-generator

# firewall: SSH, HTTP, HTTPS only (Docker only publishes 80/443 from this stack)
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp
ufw --force enable
```
Also check Contabo's panel firewall (Network > Firewall) allows those ports if you use it.

## 2. Create a deploy key (on YOUR PC, never on the server)
```bash
ssh-keygen -t ed25519 -f deploy_key -N "" -C "github-actions-deploy"
```
- Put `deploy_key.pub` on the server: append it to `/home/deploy/.ssh/authorized_keys`
  (`mkdir -p /home/deploy/.ssh`, then `chmod 700` the folder, `chmod 600` the file, `chown -R deploy:deploy`).
- The private file `deploy_key` goes into the GitHub secret below. Then delete it from your PC.
- Optional but recommended once it works: in `/etc/ssh/sshd_config` set `PasswordAuthentication no`
  and `PermitRootLogin prohibit-password`, then `systemctl restart ssh`.
- Capture the server's host key: `ssh-keyscan -t ed25519 45.140.201.44` and keep the output.

## 3. GitHub repository secrets
Repo > Settings > Secrets and variables > Actions > New repository secret:

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | `45.140.201.44` |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_SSH_KEY` | the full contents of `deploy_key` (the private key) |
| `DEPLOY_KNOWN_HOSTS` | the `ssh-keyscan` output from step 2 |
| `PROD_ENV` | your filled-in copy of `.env.production.example` (all lines) |

Generate the secrets in `PROD_ENV` with `openssl rand -hex 24` (database) and `openssl rand -hex 48`
(each JWT value). Copy the AI provider lines from your local `.env`. Do not paste any of these into chat.

## 4. Domains
- Testing: the `sslip.io` names in the example need no setup. If Let's Encrypt refuses (shared rate
  limit), use `nip.io` instead, e.g. `app.45.140.201.44.nip.io`.
- Real domain: add two A records (`app`, `api`) pointing at `45.140.201.44`, then change `APP_DOMAIN` and
  `API_DOMAIN` in `PROD_ENV` and redeploy. If you use Cloudflare, set the records to DNS only (grey cloud).

## 5. Deploy
From your PC, in the repo (needs Python 3.9+ and OpenSSH):
```
copy .env.production.example .env.production      # fill it in; it is git-ignored
python deploy/deploy.py deploy --dry-run --host 45.140.201.44 --key <path to deploy_key>
python deploy/deploy.py deploy --host 45.140.201.44 --key <path to deploy_key>
```
`--dry-run` checks the env file and the file list and connects to nothing. Other commands: `status`,
`logs [service]`, `rollback` (back to the previous release; the database is not rolled back). The same
settings can come from `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` and `DEPLOY_ENV_FILE`.

**Changing only the env file** (a new API key, SMTP settings, a log level...): edit `.env.production`, then
```
python deploy/deploy.py env --dry-run --host 45.140.201.44 --key <path to deploy_key>   # shows names that changed
python deploy/deploy.py env         --host 45.140.201.44 --key <path to deploy_key>
```
It uploads only the env file and recreates just the services whose settings changed, with no rebuild. It
prints variable names, never values. It refuses `APP_DOMAIN` / `API_DOMAIN` changes (the frontend must be
rebuilt: use `deploy`) and `POSTGRES_*` changes (the database keeps its original password). Changing the
JWT secrets signs everyone out.

Or push to `Feedback-work` (or run the workflow by hand from the Actions tab) once the five GitHub secrets
exist. The first build takes several minutes (Chromium and the Next.js build).

**Putting it behind the host's Caddy (needs root, once).** On this server the existing Caddy
(`/opt/ppt2scorm`) is the one front door for every site, so we never publish 80/443 or run another proxy.
`deploy/attach-to-caddy.sh` does the host owner's procedure: it attaches that Caddy to the `web` network
(recreating only the caddy container, a few seconds) and APPENDS two site blocks to its Caddyfile
(`APP_DOMAIN` -> `coursegen-frontend:3000`, `API_DOMAIN` -> `coursegen-backend:8000`). It backs both files
up, refuses to run if they are not in the expected shape, validates before reloading, and restores the
backups itself if anything fails. It never touches the other app, its data, the firewall or SSH.
```
scp deploy/attach-to-caddy.sh root@45.140.201.44:/root/
ssh root@45.140.201.44 "bash /root/attach-to-caddy.sh courses.45-140-201-44.sslip.io courses-api.45-140-201-44.sslip.io"
```
These `sslip.io` names resolve to the server IP by themselves, so no DNS records are needed. With your own
domain, add two A records (DNS only if on Cloudflare) to the server IP and use those names instead.
Caddy gets the certificates by itself within a minute. (sslip.io certificates share a public rate limit;
if issuance fails, wait or try again later. Use your own domain for real users.) Host rules we follow: no public ports, containers pinned to CPU cores
4-7 (the other app uses 0-3), backend limited to 6 GB, and never `docker compose down` in `/opt/ppt2scorm`.

## 6. First-run checks
1. Open `https://<APP_DOMAIN>` and `https://<API_DOMAIN>/health`.
2. Register with the `INITIAL_ADMIN_EMAIL` address immediately. That account becomes the admin.
3. Create a short course end to end, run one code block, and generate a PDF.
4. Useful commands on the server (as `deploy`, in `/opt/course-generator`):
   `docker compose -f docker-compose.prod.yml ps` and `... logs -f backend`.

## 7. Production notes
- **Open registration:** anyone who can reach the site can sign up as an Author and start generations
  that cost AI money. There is no restriction in the code. Consider basic auth or an allowlist before
  sharing the URL widely.
- **API docs:** FastAPI's `/docs` is public on the API domain by default.
- **Rate limiting:** none in the app.
- **Backups:** data lives in the `postgres_data` and `backend_data` volumes. Schedule a daily
  `docker compose exec -T postgres pg_dump -U course_app course_generator | gzip` plus a copy of the
  `backend_data` volume to storage OFF the server. Contabo's Auto Backup snapshots help but a snapshot
  of a running database may not be consistent.
- **Disk:** the deploy prunes dangling images; also watch overall disk usage.
- **Single backend worker:** do not raise `--workers` or add replicas. Job progress is held in memory.
- **A secret in `PROD_ENV` changes only on the next deploy.** Rotate by editing the secret and rerunning.
- **Releases:** each deploy goes to `/opt/course-generator/releases/<timestamp>/` (the newest 3 are kept);
  the env file lives in `/opt/course-generator/shared/.env` (mode 600). Docker volumes are never touched.

## 8. Troubleshooting
- **Workflow fails at "Configure SSH" or the first ssh:** check the three SSH secrets, and that the
  key's public half is in `/home/deploy/.ssh/authorized_keys` with correct permissions.
- **"Host key verification failed":** `DEPLOY_KNOWN_HOSTS` is empty or from a different server.
- **Site shows a certificate error:** DNS does not point at the server yet, or ports 80/443 are
  blocked; check `docker compose logs caddy`.
- **Logged out on reload:** the two domains do not share a parent, or you changed domains without a
  redeploy (the API address is baked into the frontend at build time).
- **Backend exits at start:** a missing required variable is named in its log.
