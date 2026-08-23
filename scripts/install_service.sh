#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "Installing WaveController background daemon systemd service..."

mkdir -p "$SYSTEMD_USER_DIR"

cat << EOF_UNIT > "$SYSTEMD_USER_DIR/wavecontroller.service"
[Unit]
Description=WaveController Audio Routing Daemon
Documentation=https://github.com/oparada1988/WaveController
After=pipewire.service wireplumber.service
Wants=pipewire.service wireplumber.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 "$SCRIPT_DIR/main.py" --daemon
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF_UNIT

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "Enabling and starting wavecontroller.service..."
systemctl --user enable wavecontroller.service
systemctl --user restart wavecontroller.service

echo "WaveController background daemon successfully installed and running!"
systemctl --user status wavecontroller.service --no-pager
