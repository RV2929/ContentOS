#!/bin/bash
# ContentOS — resilient Cloudflare Quick Tunnel (no domain required).
#
# Publishes http://localhost:3000 via a free trycloudflare.com "quick tunnel"
# so Buffer can fetch clip files. Quick tunnels don't need a Cloudflare
# account or domain — but the URL is random and changes every time the
# tunnel restarts. This script:
#   1. Restarts cloudflared automatically if it crashes or drops.
#   2. Captures the new URL every time it changes.
#   3. Writes it to tunnel-url.txt AND updates VIDEO_BASE_URL in .env,
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

CLOUDFLARED="$HOME/.npm-global/bin/cloudflared"
if [ ! -x "$CLOUDFLARED" ]; then
  CLOUDFLARED="$(command -v cloudflared || true)"
fi
if [ -z "$CLOUDFLARED" ] || [ ! -x "$CLOUDFLARED" ]; then
  echo "ERROR: cloudflared not found. Install it (e.g. 'brew install cloudflared') and retry." | tee -a "$LOG_FILE"
  exit 1
fi

log() { echo "$(date '+%F %T')  $1" | tee -a "$LOG_FILE"; }

log "Starting resilient quick tunnel using $CLOUDFLARED"

while true; do
  "$CLOUDFLARED" tunnel --url http://localhost:3000 --no-autoupdate 2>&1 | while IFS= read -r line; do
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
        echo "$(date '+%F %T')  Tunnel URL: $url" | tee -a "$LOG_FILE"
      fi
    fi
  done

  echo "$(date '+%F %T')  cloudflared exited — restarting in 3s…" | tee -a "$LOG_FILE"
  sleep 3
done
