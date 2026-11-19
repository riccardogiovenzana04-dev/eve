#!/bin/sh
# Start eve automatically when you log in to your Mac (a LaunchAgent).
# Usage: host/install-autostart-macos.sh [extra voicetype.py flags, e.g. --language it]
# Remove: launchctl unload ~/Library/LaunchAgents/dev.eve.voicetype.plist && rm that file
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/dev.eve.voicetype.plist"
LOG="$HOME/Library/Logs/voicetype.log"
[ -x "$PY" ] || { echo "Create the venv first: python3 -m venv host/.venv && host/.venv/bin/pip install -r host/requirements.txt"; exit 1; }

ARGS=""
for a in "$PY" "$HERE/voicetype.py" "$@"; do
  ARGS="$ARGS        <string>$a</string>
"
done

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>dev.eve.voicetype</string>
    <key>ProgramArguments</key>
    <array>
$ARGS    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed $PLIST — eve is running now and will start at every login."
echo "Log: $LOG"
echo
echo "One-time permission: System Settings > Privacy & Security > Accessibility,"
echo "click +, press Cmd+Shift+G and add: $(readlink -f "$PY" 2>/dev/null || echo "$PY")"
