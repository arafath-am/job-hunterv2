#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════
# Job Hunter v2 — Performance Fix Deployment
#
# What this does:
#   1. Applies code patches (pagination, indexes)
#   2. Splits into two services: web + collector
#   3. Web server never blocked by collection again
#
# Run from ~/job-hunterv2/:
#   bash deploy_perf.sh
# ═══════════════════════════════════════════════════════════════

cd ~/job-hunterv2

echo "═══════════════════════════════════════════"
echo " Job Hunter v2 — Deploying performance fixes"
echo "═══════════════════════════════════════════"

# ── Step 1: Apply code patches ──
echo ""
echo "[Step 1] Applying code patches..."
python3 apply_fixes.py
if [ $? -ne 0 ]; then
    echo "ERROR: Patch script failed. Aborting."
    exit 1
fi

# ── Step 2: Stop old service ──
echo ""
echo "[Step 2] Stopping old jobhunter service..."
sudo systemctl stop jobhunter || true
sudo systemctl disable jobhunter || true
echo "  ✓ Old service stopped and disabled"

# ── Step 3: Install new service files ──
echo ""
echo "[Step 3] Installing split services..."
sudo cp jobhunter-web.service /etc/systemd/system/
sudo cp jobhunter-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable jobhunter-web jobhunter-collector
echo "  ✓ Service files installed and enabled"

# ── Step 4: Start new services ──
echo ""
echo "[Step 4] Starting services..."
sudo systemctl start jobhunter-web
sleep 2
sudo systemctl start jobhunter-collector
echo "  ✓ Both services started"

# ── Step 5: Verify ──
echo ""
echo "[Step 5] Verifying..."
sleep 3
echo ""
echo "── Web service ──"
sudo systemctl status jobhunter-web --no-pager -l | head -12
echo ""
echo "── Collector service ──"
sudo systemctl status jobhunter-collector --no-pager -l | head -12

echo ""
echo "═══════════════════════════════════════════"
echo " Deployment complete."
echo ""
echo " Web server:  systemctl status jobhunter-web"
echo " Collector:   systemctl status jobhunter-collector"
echo " Web logs:    sudo journalctl -u jobhunter-web -f"
echo " Coll logs:   sudo journalctl -u jobhunter-collector -f"
echo "═══════════════════════════════════════════"
