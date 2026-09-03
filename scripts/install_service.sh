#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "Installing WaveController background daemon systemd service..."

mkdir -p "$SYSTEMD_USER_DIR"

# Deploy WirePlumber Studio Audio Profile (anti-suspend, 48kHz clock pinning)
WIREPLUMBER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d"
mkdir -p "$WIREPLUMBER_DIR"
if [ -f "$SCRIPT_DIR/data/51-wavecontroller-wave-xlr.conf" ]; then
    echo "Deploying WaveController WirePlumber configuration..."
    cp "$SCRIPT_DIR/data/51-wavecontroller-wave-xlr.conf" "$WIREPLUMBER_DIR/51-wavecontroller-wave-xlr.conf"
fi

cat << EOF_UNIT > "$SYSTEMD_USER_DIR/wavecontroller.service"
[Unit]
Description=WaveController Audio Routing Daemon
Documentation=https://github.com/oparada1988/WaveController
After=graphical-session.target pipewire.service wireplumber.service
Wants=pipewire.service wireplumber.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 "$SCRIPT_DIR/main.py" --daemon
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical-session.target
EOF_UNIT

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "Enabling and starting wavecontroller.service..."
systemctl --user enable wavecontroller.service
systemctl --user restart wavecontroller.service

echo "WaveController background daemon successfully installed and running!"
systemctl --user status wavecontroller.service --no-pager
