#!/bin/bash
# One-time setup: installs the ContentOS tunnel as a PM2-managed background
# process so it auto-restarts if it ever crashes — no Cloudflare domain
# or account needed.
#
# Why PM2 instead of launchd: ~/Desktop is a macOS-protected folder (TCC).
# When launchd spawns /bin/bash directly, macOS blocks it from touching
# anything under Desktop ("Operation not permitted"), even though the exact
# same script runs fine from Terminal. PM2 is already running the dashboard
# out of this same folder without issue, so we piggyback on that.
#
# Run once:  bash install-tunnel-service.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PM2="$(command -v pm2 || echo "$HOME/.npm-global/bin/pm2")"

if [ ! -x "$PM2" ] && ! command -v pm2 >/dev/null 2>&1; then
  echo "ERROR: pm2 not found. Install it first: npm install -g pm2"
  exit 1
fi

chmod +x "$SCRIPT_DIR/run-tunnel.sh"

# Clean slate if it was already registered
pm2 delete contentos-tunnel 2>/dev/null || true

pm2 start "$SCRIPT_DIR/run-tunnel.sh" \
  --name contentos-tunnel \
  --interpreter bash \
  --restart-delay 3000

pm2 save

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ContentOS tunnel service installed (via PM2)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "PM2 will now keep the tunnel running and restart it automatically if"
echo "it ever crashes. .env's VIDEO_BASE_URL and tunnel-url.txt stay up to"
echo "date automatically as the tunnel URL changes."
echo ""
echo "  Check it's running:  pm2 list"
echo "  Current tunnel URL:  cat $SCRIPT_DIR/tunnel-url.txt"
echo "  View logs:           pm2 logs contentos-tunnel"
echo "  Stop it:             pm2 delete contentos-tunnel"
echo ""
echo "To also survive a full Mac reboot (not just crashes), run this ONE-TIME"
echo "command if you haven't already set up PM2's startup script:"
echo ""
echo "  pm2 startup"
echo ""
echo "It will print a sudo command tailored to your Mac — run that, then:"
echo ""
echo "  pm2 save"
echo ""
