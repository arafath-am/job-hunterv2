#!/bin/bash
# deploy_session5.sh — Deploy Session 5 changes to the VM
# Run from ~/job-hunterv2 on the VM
set -e

echo "=== Session 5: Re-discovery + Endpoint Fixes ==="
echo ""

# ── 1. Fix discover.py endpoints ──
echo "[1/3] Fixing discover.py endpoints..."
if grep -q 'posting-api/posting-board' tools/discover.py 2>/dev/null; then
    sed -i 's|posting-api/posting-board|posting-api/job-board|g' tools/discover.py
    echo "  ✓ Ashby endpoint fixed (posting-board → job-board)"
else
    echo "  ✓ Ashby endpoint already correct"
fi
if grep -q 'api/v3/accounts' tools/discover.py 2>/dev/null; then
    sed -i "s|api/v3/accounts/\(.*\)/jobs|api/v1/widget/accounts/\1|" tools/discover.py
    echo "  ✓ Workable endpoint fixed (v3 → v1/widget)"
else
    echo "  ✓ Workable endpoint already correct"
fi

# ── 2. Run manual resolution ──
echo ""
echo "[2/3] Resolving companies from manual research..."
python3 migrations/resolve_identified.py --dry-run
echo ""
read -p "Apply these changes? (y/n): " confirm
if [ "$confirm" = "y" ]; then
    python3 migrations/resolve_identified.py
else
    echo "  Skipped. Run manually later: python3 migrations/resolve_identified.py"
fi

# ── 3. Restart collector ──
echo ""
echo "[3/3] Restarting collector..."
sudo systemctl restart jobhunter-collector
echo "  ✓ Done"

echo ""
echo "=== Next steps ==="
echo "  • Wait ~25 min, then check: sudo journalctl -u jobhunter-collector --since '30 min ago' --no-pager | tail -30"
echo "  • Count jobs: python3 -c \"import sqlite3; c=sqlite3.connect('jobhunter.db'); [print(r) for r in c.execute('SELECT ats,COUNT(*) FROM jobs WHERE active=1 GROUP BY ats ORDER BY COUNT(*) DESC')]\""
echo ""
echo "  For remaining identified companies (after finding more URLs tomorrow):"
echo "    python3 tools/rediscover.py --dry-run     # uses Serper API"
echo "    python3 tools/rediscover.py               # apply"
echo ""
echo "  Commit: git add -A && git commit -m 'Session 5: resolve identified companies' && git push"
