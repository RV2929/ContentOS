#!/bin/bash
# ContentOS — resilient Cloudflare Quick Tunnel (no domain required).
#
# Publishes http://localhost:3000 via a free trycloudflare.com "quick tunnel"
# so Buffer can fetch clip files. Quick tunnels don't need a Cloudflare
# account or domain — but the URL is random and changes every time the
# tunnel restarts. This script:
#   1. Restarts cloudflared automatically if it crashes or drops.
#   2. Runs a background health check that curls the tunnel's own public URL
#      every 60s and force-kills cloudflared if it hangs or stops answering,
#      since a hung/half-dead process won't exit on its own and PM2 can't
#      see that anything is wrong.
#   3. Captures the new URL every time it changes.
#   4. Writes it to tunnel-url.txt AND updates VIDEO_BASE_URL in .env,
#      so buffer_poster.py always has a working link — no manual edits.
#
# Run directly for a one-off session:
#   bash run-tunnel.sh
#
# For a "permanent" tunnel that survives crashes and Mac reboots, install it
# as a background service instead (one-time setup):
#   bash install-tunnel-service.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
URL_FILE="$SCRIPT_DIR/tunnel-url.txt"
LOG_FILE="$SCRIPT_DIR/tunnel.log"
ENV_FILE="$SCRIPT_DIR/.env"
PID_FILE="$SCRIPT_DIR/.cloudflared.pid"

HEALTH_CHECK_INTERVAL=60
HEALTH_CHECK_TIMEOUT=10

CLOUDFLARED="$HOME/.npm-global/bin/cloudflared"
if [ ! -x "$CLOUDFLARED" ]; then
  CLOUDFLARED="$(command -v cloudflared || true)"
fi
if [ -z "$CLOUDFLARED" ] || [ ! -x "$CLOUDFLARED" ]; then
  echo "ERROR: cloudflared not found. Install it (e.g. 'brew install cloudflared') and retry." | tee -a "$LOG_FILE"
  exit 1
fi

log() { echo "$(date '+%F %T')  $1" | tee -a "$LOG_FILE"; }

# Periodically curls the tunnel's own last-known public URL. If it doesn't
# come back with a 200 (non-200, timeout, or DNS failure all show up as a
# non-"200" http_code from curl), the tunnel is dead even though the
# cloudflared process may still be running/hung — so kill it and let the
# main loop below restart it with a fresh URL.
health_check_loop() {
  while true; do
    sleep "$HEALTH_CHECK_INTERVAL"

    url=""
    [ -f "$URL_FILE" ] && url="$(cat "$URL_FILE" 2>/dev/null)"
    [ -z "$url" ] && continue

    http_code="$(curl -sS -o /dev/null -w '%{http_code}' \
      --max-time "$HEALTH_CHECK_TIMEOUT" --connect-timeout 5 "$url" 2>/dev/null)"

    if [ "$http_code" != "200" ]; then
      log "HEALTH CHECK FAILED: $url returned '${http_code:-no response}' — killing cloudflared to force restart"

      pid=""
      [ -f "$PID_FILE" ] && pid="$(cat "$PID_FILE" 2>/dev/null)"
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null
        sleep 2
        if kill -0 "$pid" 2>/dev/null; then
          log "cloudflared (pid $pid) ignored SIGTERM — sending SIGKILL"
          kill -KILL "$pid" 2>/dev/null
        fi
      fi
    fi
  done
}

health_check_loop &
HEALTH_CHECK_PID=$!

cleanup() {
  kill "$HEALTH_CHECK_PID" 2>/dev/null
  pid=""
  [ -f "$PID_FILE" ] && pid="$(cat "$PID_FILE" 2>/dev/null)"
  [ -n "$pid" ] && kill "$pid" 2>/dev/null
  rm -f "$PID_FILE"
}
trap cleanup EXIT INT TERM

log "Starting resilient quick tunnel using $CLOUDFLARED (health check every ${HEALTH_CHECK_INTERVAL}s)"

while true; do
  "$CLOUDFLARED" tunnel --url http://localhost:3000 --no-autoupdate > >(
    while IFS= read -r line; do
      echo "$line" >> "$LOG_FILE"
      if [[ "$line" == *"trycloudflare.com"* ]]; then
        url=$(echo "$line" | grep -oE 'https://[a-zA-Z0-9.-]*\.trycloudflare\.com' | head -n1)
        if [ -n "$url" ]; then
          echo "$url" > "$URL_FILE"
          if [ -f "$ENV_FILE" ]; then
            if grep -q '^VIDEO_BASE_URL=' "$ENV_FILE"; then
              sed -i.bak "s|^VIDEO_BASE_URL=.*|VIDEO_BASE_URL=$url|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
            else
              printf '\nVIDEO_BASE_URL=%s\n' "$url" >> "$ENV_FILE"
            fi
          fi
          log "Tunnel URL: $url"
        fi
      fi
    done
  ) 2>&1 &
  CLOUDFLARED_PID=$!
  echo "$CLOUDFLARED_PID" > "$PID_FILE"

  wait "$CLOUDFLARED_PID"

  rm -f "$PID_FILE"
  log "cloudflared exited — restarting in 3s…"
  sleep 3
done
