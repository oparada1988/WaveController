import os
import shutil
import subprocess
from .logger import get_logger

logger = get_logger("Autostart")

SERVICE_NAME = "wavecontroller.service"
SYSTEMD_USER_DIR = os.path.expanduser("~/.config/systemd/user")
SERVICE_FILE = os.path.join(SYSTEMD_USER_DIR, SERVICE_NAME)

# Legacy XDG autostart entry, superseded by the systemd user service below.
LEGACY_AUTOSTART_FILE = os.path.expanduser("~/.config/autostart/com.oparada.WaveController.desktop")


def get_executable_path() -> str:
    """Determines the best executable command for launching WaveController in background."""
    bin_path = os.path.expanduser("~/.local/bin/wavecontroller")
    if os.path.exists(bin_path):
        return f"{bin_path} --daemon"
    which_path = shutil.which("wavecontroller")
    if which_path:
        return f"{which_path} --daemon"
    local_main = os.path.expanduser("~/.local/share/wavecontroller/main.py")
    if os.path.exists(local_main):
        return f"/usr/bin/python3 {local_main} --daemon"
    return "/usr/bin/python3 -m wavecontroller.main --daemon"


def _cleanup_legacy_autostart() -> None:
    """Removes the old XDG autostart desktop entry left over from pre-systemd versions."""
    if os.path.exists(LEGACY_AUTOSTART_FILE):
        try:
            os.remove(LEGACY_AUTOSTART_FILE)
            logger.info(f"Removed legacy autostart entry: {LEGACY_AUTOSTART_FILE}")
        except Exception as e:
            logger.warning(f"Failed to remove legacy autostart entry: {e}")


def _write_service_unit() -> None:
    """Writes/refreshes the user-space systemd unit so it always points at the current install."""
    os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
    exec_cmd = get_executable_path()
    content = f"""[Unit]
Description=WaveController Audio Routing Daemon
Documentation=https://github.com/oparada1988/WaveController
After=graphical-session.target pipewire.service wireplumber.service
Wants=pipewire.service wireplumber.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={exec_cmd}
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical-session.target
"""
    with open(SERVICE_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


def is_autostart_enabled() -> bool:
    """Checks if the WaveController systemd user service is enabled."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", SERVICE_NAME],
            capture_output=True, text=True, check=False,
        )
        return result.stdout.strip() == "enabled"
    except Exception as e:
        logger.warning(f"Failed to query systemd autostart state: {e}")
        return False


def set_autostart_enabled(enabled: bool) -> bool:
    """Enables or disables the WaveController systemd user service (user-space unit, no sudo required)."""
    _cleanup_legacy_autostart()
    try:
        if enabled:
            _write_service_unit()
            subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME], check=True)
            logger.info("Autostart enabled via systemd user service")
        else:
            subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], check=False)
            logger.info("Autostart disabled via systemd user service")
        return True
    except Exception as e:
        logger.error(f"Failed to update autostart setting ({enabled}): {e}")
        return False
