import os
import shutil
from .logger import get_logger

logger = get_logger("Autostart")

AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "com.oparada.WaveController.desktop")

def is_autostart_enabled() -> bool:
    """Checks if WaveController autostart desktop entry exists and is enabled."""
    if not os.path.isfile(AUTOSTART_FILE):
        return False
    try:
        with open(AUTOSTART_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().lower() == "x-gnome-autostart-enabled=false":
                    return False
        return True
    except Exception as e:
        logger.warning(f"Failed to read autostart file: {e}")
        return False

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

def set_autostart_enabled(enabled: bool) -> bool:
    """Creates or removes the XDG autostart desktop entry."""
    try:
        if enabled:
            os.makedirs(AUTOSTART_DIR, exist_ok=True)
            exec_cmd = get_executable_path()
            content = f"""[Desktop Entry]
Type=Application
Name=WaveController
GenericName=Audio Mixer
Comment=Elgato Wave Link & Advanced Multi-Track Virtual Mixer for Linux
Exec={exec_cmd}
Icon=com.oparada.WaveController
Terminal=false
Categories=AudioVideo;Audio;Mixer;GTK;
StartupWMClass=com.oparada.WaveController
X-GNOME-Autostart-enabled=true
"""
            with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"Autostart enabled: created {AUTOSTART_FILE}")
            return True
        else:
            if os.path.exists(AUTOSTART_FILE):
                os.remove(AUTOSTART_FILE)
                logger.info(f"Autostart disabled: removed {AUTOSTART_FILE}")
            return True
    except Exception as e:
        logger.error(f"Failed to update autostart setting ({enabled}): {e}")
        return False
