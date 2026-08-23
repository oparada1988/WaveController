#!/usr/bin/env bash
set -e

SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PIPEWIRE_CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/WaveController"

echo "Uninstalling WaveController..."

# 1. Stop and disable background service
echo "Stopping and disabling background daemon service..."
systemctl --user stop wavecontroller.service 2>/dev/null || true
systemctl --user disable wavecontroller.service 2>/dev/null || true
rm -f "$SYSTEMD_USER_DIR/wavecontroller.service"
systemctl --user daemon-reload 2>/dev/null || true

# 2. Remove PipeWire drop-in configs
echo "Removing PipeWire configuration drop-ins..."
rm -f "$PIPEWIRE_CONF_DIR/99-wavecontroller.conf"
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire-pulse.conf.d/99-wavecontroller.conf"

# 3. Clean up WaveController configuration & socket directory
echo "Removing WaveController configuration & IPC socket directory..."
rm -rf "$CONFIG_DIR"

# 4. Remove desktop entries if installed
rm -f "${HOME}/.local/share/applications/com.oparada.WaveController.desktop"

# 5. Restart PipeWire to cleanly flush virtual audio devices
echo "Restarting PipeWire audio subsystem..."
systemctl --user restart pipewire pipewire-pulse wireplumber 2>/dev/null || true

echo "WaveController has been completely uninstalled and your PipeWire audio subsystem restored to default."
