#!/usr/bin/env bash
# Deploy path2v2x/v2x-drive to path-rfs.
#
#   scripts/deploy.sh            site only (static build + nginx + config)
#   scripts/deploy.sh --server   also restart the CARLA drive server (skipped
#                                with exit 2 while a drive session is active)
#   scripts/deploy.sh --dry-run  print the plan without touching the host
#
# Everything is served from one host: nginx serves the built site from
# /var/www/v2x-drive, proxies /ws to the drive server, /camera and /archive to
# MediaMTX, and /detections to the twin server. The checkout on the host is
# fast-forwarded to origin/main, so push before deploying.
set -euo pipefail

HOST="${DEPLOY_HOST:-root@100.126.56.83}"
APP_USER=path
CHECKOUT=/home/path/v2x-drive
SITE_ROOT=/var/www/v2x-drive
DATA_ROOT=/var/www/v2x-drive-data
PUBLIC_URL="${DEPLOY_PUBLIC_URL:-https://path2v2x.net}"

restart_server=0
dry_run=0
for arg in "$@"; do
  case "$arg" in
    --server) restart_server=1 ;;
    --dry-run) dry_run=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 64 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
local_head="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  echo "warning: local working tree has uncommitted changes; the host deploys origin/main ($local_head is local HEAD)" >&2
fi

step() { printf '\n== %s\n' "$1"; }
remote() { ssh -o BatchMode=yes "$HOST" "$@"; }
remote_as_app() { remote su - "$APP_USER" -c "\"$*\""; }

plan=(
  "fast-forward $CHECKOUT to origin/main as $APP_USER"
  "npm ci in apps/drive-web when package-lock.json changed"
  "npm run build in apps/drive-web"
  "rsync apps/drive-web/build/ -> $SITE_ROOT/ (config.json ships with the build)"
  "install scripts/ops/nginx/{v2x-drive-public,acme-http}.conf, nginx -t, reload"
  "ensure $DATA_ROOT/{api,snapshots,demo-videos} exist for the drive server"
  "health: $PUBLIC_URL/ 200, /config.json has detectionsBaseUrl, /detections/coverage 200"
)
if [ "$restart_server" = 1 ]; then
  plan+=("restart v2x-drive.service when no drive session is connected")
fi
step "Plan ($HOST)"
printf '  - %s\n' "${plan[@]}"
[ "$dry_run" = 1 ] && exit 0

step "Fast-forward checkout"
before="$(remote_as_app git -C "$CHECKOUT" rev-parse HEAD)"
remote_as_app git -C "$CHECKOUT" pull -q --ff-only origin main
after="$(remote_as_app git -C "$CHECKOUT" rev-parse HEAD)"
echo "  $before -> $after"

step "Build drive-web"
if [ "$before" = "$after" ] || remote_as_app git -C "$CHECKOUT" diff --quiet "$before" "$after" -- apps/drive-web/package-lock.json; then
  echo "  lockfile unchanged; skipping npm ci"
else
  remote_as_app "cd $CHECKOUT/apps/drive-web && npm ci --no-audit --no-fund --silent"
fi
remote_as_app "cd $CHECKOUT/apps/drive-web && npm run build --silent"

step "Publish site"
remote "rsync -a --delete $CHECKOUT/apps/drive-web/build/ $SITE_ROOT/ && chown -R www-data:www-data $SITE_ROOT"
remote "install -o $APP_USER -g $APP_USER -d $DATA_ROOT $DATA_ROOT/api $DATA_ROOT/snapshots $DATA_ROOT/demo-videos"

step "nginx"
remote "install -m 644 $CHECKOUT/scripts/ops/nginx/v2x-drive-public.conf /etc/nginx/sites-available/v2x-drive-public && install -m 644 $CHECKOUT/scripts/ops/nginx/acme-http.conf /etc/nginx/sites-available/acme-http && nginx -t && systemctl reload nginx"

if [ "$restart_server" = 1 ]; then
  step "Drive server"
  sessions="$(remote "ss -Htn state established '( sport = :8765 )' | wc -l")"
  if [ "$sessions" != "0" ]; then
    echo "  $sessions drive session(s) connected; not restarting v2x-drive. Re-run with --server later." >&2
    exit 2
  fi
  remote "install -m 644 $CHECKOUT/scripts/systemd/v2x-drive.service /etc/systemd/system/v2x-drive.service && systemctl daemon-reload && systemctl restart v2x-drive"
  echo "  v2x-drive restarted; CARLA takes a few minutes to come back"
fi

step "Health"
code="$(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC_URL/")"
[ "$code" = 200 ] || { echo "  $PUBLIC_URL/ returned $code" >&2; exit 1; }
curl -sf "$PUBLIC_URL/config.json" | grep -q '"detectionsBaseUrl"' || { echo "  config.json lacks detectionsBaseUrl" >&2; exit 1; }
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
earlier="$(date -u -d '-10 minutes' +%Y-%m-%dT%H:%M:%SZ)"
curl -sf "$PUBLIC_URL/detections/coverage?start=$earlier&end=$now&bucket=300" >/dev/null || { echo "  /detections/coverage failed" >&2; exit 1; }
echo "  site $code, config ok, detections ok"
echo "deployed $after"
