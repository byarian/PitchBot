#!/usr/bin/env bash
# PitchBot first-run setup.
# Usage: bash setup.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== PitchBot Setup ==="

# 1. Python virtualenv
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "[1/4] Creating Python virtualenv…"
    python3 -m venv "$SCRIPT_DIR/venv"
else
    echo "[1/4] Virtualenv already exists — skipping."
fi
source "$SCRIPT_DIR/venv/bin/activate"

# 2. Install dependencies
echo "[2/4] Installing dependencies…"
pip install --upgrade pip -q
pip install -r "$SCRIPT_DIR/requirements.txt" -q
echo "      Dependencies installed."

# 3. Init database
echo "[3/4] Initialising database…"
python - <<'EOF'
import sys; sys.path.insert(0, '.')
from database import init_db
init_db()
print("      Database ready.")
EOF

# 4. Desktop launcher
echo "[4/4] Installing desktop launcher…"
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
# Patch path in desktop file to this install location
sed "s|/home/user/PitchBot|$SCRIPT_DIR|g" "$SCRIPT_DIR/PitchBot.desktop" \
    > "$DESKTOP_DIR/PitchBot.desktop"
chmod +x "$DESKTOP_DIR/PitchBot.desktop"
echo "      Desktop file installed to $DESKTOP_DIR/PitchBot.desktop"

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo ""
echo "  1. Fetch 2025 season data (first-time, may take 10-20 min):"
echo "       source venv/bin/activate"
echo "       python update.py --full-season"
echo ""
echo "  2. Launch PitchBot:"
echo "       bash start.sh"
echo "     or double-click the PitchBot icon in your applications menu."
echo ""
echo "  3. Set up nightly auto-update (optional):"
echo "       crontab -e"
echo "     Add this line:"
echo "       0 4 * * * cd $SCRIPT_DIR && venv/bin/python update.py >> logs/update.log 2>&1"
echo ""
