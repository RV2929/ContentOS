#!/bin/bash
# ContentOS — Cloudflare Tunnel setup
# Run once: bash setup-tunnel.sh
# Requires: domain managed by Cloudflare (cloudflare.com/products/registrar)

set -e

CLOUDFLARED="$HOME/.npm-global/bin/cloudflared"
PM2="$HOME/.npm-global/bin/pm2"
TUNNEL_NAME="contentos-dashboard"
CONFIG_FILE="$HOME/.cloudflared/contentos-config.yml"

echo ""
echo "=== ContentOS Cloudflare Tunnel Setup ==="
echo ""

# ── Step 1: Login ────────────────────────────────────────────────────────────
echo "Step 1/4: Opening Cloudflare login in your browser…"
echo "Authorise the app, then return here."
echo ""
$CLOUDFLARED tunnel login

# ── Step 2: Create tunnel ─────────────────────────────────────────────────────
echo ""
echo "Step 2/4: Creating tunnel '${TUNNEL_NAME}'…"
# If the tunnel already exists this is a no-op
$CLOUDFLARED tunnel create "$TUNNEL_NAME" 2>/dev/null || echo "(tunnel already exists — continuing)"

# Extract the tunnel UUID
TUNNEL_ID=$($CLOUDFLARED tunnel list 2>/dev/null \
  | awk -v name="$TUNNEL_NAME" '$0 ~ name {print $1}')

if [ -z "$TUNNEL_ID" ]; then
  echo "ERROR: Could not determine tunnel ID. Run '$CLOUDFLARED tunnel list' to inspect."
  exit 1
fi

echo "Tunnel ID: $TUNNEL_ID"

# ── Step 3: Ask for hostname ──────────────────────────────────────────────────
echo ""
echo "Step 3/4: Choose a public hostname for your dashboard."
echo "  Your domain must already be on Cloudflare (cloudflare.com/products/registrar)."
echo "  Example: contentos.yourdomain.com"
echo ""
read -rp "Hostname: " HOSTNAME

if [ -z "$HOSTNAME" ]; then
  echo "ERROR: Hostname cannot be empty."
  exit 1
fi

# ── Write config ───────────────────────────────────────────────────────────────
cat > "$CONFIG_FILE" << EOF
tunnel: ${TUNNEL_ID}
credentials-file: ${HOME}/.cloudflared/${TUNNEL_ID}.json

ingress:
  - hostname: ${HOSTNAME}
    service: http://localhost:3000
  - service: http_status:404
EOF

echo "Config written to $CONFIG_FILE"

# ── Step 4: Route DNS + add to PM2 ───────────────────────────────────────────
echo ""
echo "Step 4/4: Creating DNS record in Cloudflare for ${HOSTNAME}…"
$CLOUDFLARED tunnel route dns "$TUNNEL_NAME" "$HOSTNAME"

echo ""
echo "Adding tunnel to PM2…"
# Stop existing tunnel if it's running
$PM2 delete contentos-tunnel 2>/dev/null || true

$PM2 start "$CLOUDFLARED" \
  --name contentos-tunnel \
  --restart-delay 3000 \
  -- tunnel --config "$CONFIG_FILE" run

# Restart dashboard server with PUBLIC_URL so OAuth callback uses the real domain
$PM2 restart contentos-dashboard \
  --update-env \
  --env PUBLIC_URL="https://${HOSTNAME}"

$PM2 save

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete!"
echo ""
echo "  Dashboard:  https://${HOSTNAME}"
echo "  PM2 status: pm2 list"
echo "  Tunnel logs: pm2 logs contentos-tunnel"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "IMPORTANT — Final step:"
echo "  If you haven't already, run the PM2 startup command so both"
echo "  the server and tunnel survive Mac reboots:"
echo ""
echo "  sudo env PATH=\$PATH:/usr/local/bin \\"
echo "    /Users/rajvi/.npm-global/lib/node_modules/pm2/bin/pm2 \\"
echo "    startup launchd -u rajvi --hp /Users/rajvi"
echo ""
