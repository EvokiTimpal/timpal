#!/bin/bash
# TIMPAL — Mac auto-start setup
# Installs a launchd agent so your node starts automatically on login.

PLIST="$HOME/Library/LaunchAgents/org.timpal.node.plist"
PYTHON=$(which python3)
SCRIPT=$(pwd)/timpal.py

cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>org.timpal.node</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.timpal.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.timpal.log</string>
</dict>
</plist>
EOF

launchctl load "$PLIST"
echo ""
echo "  ✓ TIMPAL node will now start automatically on login."
echo "  Log file: ~/.timpal.log"
echo ""
echo "  To stop auto-start:"
echo "  launchctl unload ~/Library/LaunchAgents/org.timpal.node.plist"
