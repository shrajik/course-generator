#!/usr/bin/env bash
# One-time, run as ROOT on the server: puts the course app behind the existing Caddy (the one front door
# for the host), following the host owner's procedure. It changes ONLY:
#   * the `caddy` service in /opt/ppt2scorm/docker-compose.yml  (adds `networks: [default, web]` and a
#     top-level external network `web`), then recreates ONLY the caddy container;
#   * /opt/ppt2scorm/Caddyfile  (APPENDS two site blocks; the existing blocks are not edited).
# It never touches the ppt2scorm service, its data, the firewall, or SSH, and never runs `down`.
#
#   bash attach-to-caddy.sh app.example.com api.example.com
#
# Safe to re-run. If anything fails after a change, the backups are restored automatically.
# DRY_RUN=1 PPT_DIR=<folder with copies of the two files> shows the result without running docker.
set -euo pipefail

APP_DOMAIN="${1:-}"; API_DOMAIN="${2:-}"
DIR="${PPT_DIR:-/opt/ppt2scorm}"
DRY="${DRY_RUN:-0}"
FRONT="coursegen-frontend:3000"   # network aliases defined in the course app's compose file
BACK="coursegen-backend:8000"
STAMP="$(date +%Y%m%d-%H%M%S)"

die() { echo "ERROR: $*" >&2; exit 1; }
[[ -n "$APP_DOMAIN" && -n "$API_DOMAIN" ]] || die "usage: bash attach-to-caddy.sh <app-domain> <api-domain>"
[[ "$APP_DOMAIN$API_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || die "domains may contain only letters, digits, dots and dashes"
[[ "$APP_DOMAIN" != "$API_DOMAIN" ]] || die "the two domains must differ"
[[ "$DRY" == "1" || "$(id -u)" -eq 0 ]] || die "run as root"
cd "$DIR"
[[ -f docker-compose.yml && -f Caddyfile ]] || die "docker-compose.yml / Caddyfile not found in $DIR"

COMPOSE_CHANGED=0; CADDYFILE_CHANGED=0
restore() {
  echo "restoring backups..." >&2
  if [[ "$CADDYFILE_CHANGED" == 1 ]]; then cat "Caddyfile.bak-web-$STAMP" > Caddyfile; fi   # cat > keeps the bind-mounted inode
  if [[ "$COMPOSE_CHANGED" == 1 ]]; then cp "docker-compose.yml.bak-web-$STAMP" docker-compose.yml; fi
  if [[ "$DRY" != "1" ]]; then docker compose up -d caddy >/dev/null 2>&1 || true; docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true; fi
}
trap 'rc=$?; if [[ $rc -ne 0 ]]; then restore; fi' EXIT

# ---- 1. the shared network
if [[ "$DRY" != "1" ]]; then docker network inspect web >/dev/null 2>&1 || docker network create web >/dev/null; fi

# ---- 2. caddy joins the network (only if it has not already)
if grep -qE '^\s*web:\s*$' docker-compose.yml && grep -qE 'networks:\s*\[default, web\]' docker-compose.yml; then
  echo "compose: caddy is already attached to web - skipping"
else
  # Only edit the exact shape we were told about: caddy is the last service and its last line is depends_on.
  grep -qE '^  caddy:' docker-compose.yml || die "no caddy service found; edit by hand"
  grep -qE '^networks:' docker-compose.yml && die "the file already has a top-level networks: section; edit by hand"
  last="$(grep -v '^\s*$' docker-compose.yml | tail -n1)"
  [[ "$last" =~ ^\ \ \ \ depends_on:\ \[ppt2scorm\]\s*$ ]] || die "caddy is not the last service ending in 'depends_on: [ppt2scorm]' (last line: '$last'); edit by hand"
  cp docker-compose.yml "docker-compose.yml.bak-web-$STAMP"; COMPOSE_CHANGED=1
  printf '%s\n' '    networks: [default, web]' >> docker-compose.yml
  printf '\n%s\n%s\n%s\n' 'networks:' '  web:' '    external: true' >> docker-compose.yml
  echo "compose: added the web network to caddy (backup: docker-compose.yml.bak-web-$STAMP)"
fi
if [[ "$DRY" != "1" ]]; then
  docker compose config --quiet
  docker compose up -d caddy          # recreates ONLY caddy (a few seconds); ppt2scorm is not touched
fi

# ---- 3. append our two site blocks (the existing blocks are not edited)
add_block() {  # $1 domain  $2 target  $3 extra
  if grep -qE "^\s*$1\s*\{" Caddyfile; then echo "Caddyfile: $1 already present - skipping"; return; fi
  if [[ "$CADDYFILE_CHANGED" == 0 ]]; then cp Caddyfile "Caddyfile.bak-web-$STAMP"; CADDYFILE_CHANGED=1; fi
  { printf '\n%s {\n    encode gzip\n' "$1"
    if [[ -n "$3" ]]; then printf '    reverse_proxy %s {\n        %s\n    }\n' "$2" "$3"; else printf '    reverse_proxy %s\n' "$2"; fi
    printf '}\n'; } >> Caddyfile
  echo "Caddyfile: added $1 -> $2"
}
add_block "$APP_DOMAIN" "$FRONT" ""
add_block "$API_DOMAIN" "$BACK" "flush_interval -1"

# ---- 4. validate, then reload (no downtime)
if [[ "$DRY" != "1" ]]; then
  docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile
  docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
fi
trap - EXIT
echo
echo "Done. Add DNS A records ($APP_DOMAIN and $API_DOMAIN -> this server's IP; Cloudflare: DNS only)."
echo "Then check https://$APP_DOMAIN and that the other site still works."
echo "Undo: cp docker-compose.yml.bak-web-$STAMP docker-compose.yml; cat Caddyfile.bak-web-$STAMP > Caddyfile; docker compose up -d caddy; docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile"
