#!/bin/bash
# TIMPAL — Linux auto-start setup
# Installs a systemd user service so your node starts automatically on login.

PYTHON=$(which python3)
SCRIPT=$(pwd)/timpal.py
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE="$SERVICE_DIR/timpal.service"

mkdir -p "$SERVICE_DIR"

cat > "$SERVICE" << EOF
[Unit]
Description=TIMPAL Node
After=network.target

[Service]
ExecStart=$PYTHON $SCRIPT
Restart=always
StandardOutput=append:%h/.timpal.log
StandardError=append:%h/.timpal.log

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable timpal
systemctl --user start timpal

echo ""
echo "  ✓ TIMPAL node will now start automatically on login."
echo "  Log file: ~/.timpal.log"
echo ""
echo "  To stop auto-start:"
echo "  systemctl --user disable timpal"
