#!/bin/bash
# ================================================================
#  deploy_to_router.sh — Copy HomeControl files to GL-SFT1200
# ================================================================
#  Run this from your Windows PC (Git Bash / WSL / MSYS2):
#    bash deploy_to_router.sh
#
#  Or run the commands one at a time via SSH if you prefer.
# ================================================================

ROUTER="root@192.168.8.1"

set -e

echo "=== 1. Copying web UI to /www/homecontrol/ ==="
ssh "$ROUTER" "mkdir -p /www/homecontrol"
scp router/www/index.html "$ROUTER":/www/homecontrol/index.html

echo ""
echo "=== 2. Copying CGI script to /www/cgi-bin/ ==="
scp router/cgi-bin/govee "$ROUTER":/www/cgi-bin/govee

echo ""
echo "=== 3. Setting executable permission on CGI script ==="
ssh "$ROUTER" "chmod +x /www/cgi-bin/govee"

echo ""
echo "=== 4. Verifying uhttpd CGI configuration ==="
ssh "$ROUTER" '
  # Check current cgi_prefix — should be /cgi-bin
  CURRENT=$(uci get uhttpd.main.cgi_prefix 2>/dev/null || echo "(not set)")
  echo "   Current cgi_prefix: $CURRENT"

  if [ "$CURRENT" != "/cgi-bin" ]; then
    echo "   -> Setting cgi_prefix to /cgi-bin"
    uci set uhttpd.main.cgi_prefix="/cgi-bin"
    uci commit uhttpd
    echo "   -> Restarting uhttpd"
    /etc/init.d/uhttpd restart
    echo "   -> Done"
  else
    echo "   -> Already correct, no changes needed"
  fi
'

echo ""
echo "=== 5. Verifying deployment ==="
ssh "$ROUTER" '
  echo "   CGI script:"
  ls -la /www/cgi-bin/govee
  echo ""
  echo "   Web UI:"
  ls -la /www/homecontrol/index.html
  echo ""
  echo "   State file location: /tmp/govee_state.json"
  echo "   (will be created on first API call)"
'

echo ""
echo "================================================================"
echo "  Deployment complete!"
echo ""
echo "  Web UI:    http://192.168.8.1/homecontrol/"
echo "  CGI test:  curl http://192.168.8.1/cgi-bin/govee?cmd=status"
echo "  API docs:  http://192.168.8.118:8000/docs  (when PC is on)"
echo "================================================================"
