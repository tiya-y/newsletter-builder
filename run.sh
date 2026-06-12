#!/bin/bash
# Newsletter Builder — startup script

cd "$(dirname "$0")"

echo "📦 Installing dependencies..."
pip install -r requirements.txt --break-system-packages -q 2>/dev/null || pip install -r requirements.txt -q

echo ""
echo "🗞  Starting Newsletter Builder at http://localhost:5050"
echo "    Press Ctrl+C to stop."
echo ""

# Open browser after a short delay
(sleep 1.5 && open http://localhost:5050 2>/dev/null || xdg-open http://localhost:5050 2>/dev/null) &

python3 server.py
