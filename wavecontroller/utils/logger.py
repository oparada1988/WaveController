import os
import shutil
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

LOG_DIR = os.path.expanduser("~/.config/WaveController/logs")
LOG_FILE = os.path.join(LOG_DIR, "wavecontroller.log")
_INITIALIZED = False

def setup_logging(level=logging.INFO):
    """Configures centralized rotating file logging and console logging for WaveController."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    
    os.makedirs(LOG_DIR, exist_ok=True)
    
    root_logger = logging.getLogger("WaveController")
    root_logger.setLevel(level)
    
    # Remove existing handlers if any
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. Rotating File Handler (Max 5 MB per file, keep up to 3 backups)
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root_logger.addHandler(file_handler)

    # 2. Console Stream Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    root_logger.addHandler(console_handler)

    _INITIALIZED = True
    root_logger.info("=== WaveController Logging Subsystem Initialized ===")
    root_logger.info(f"Log path: {LOG_FILE}")

def get_logger(name: str = "WaveController") -> logging.Logger:
    """Returns a namespaced logger under WaveController root hierarchy."""
    if not _INITIALIZED:
        setup_logging()
    if name.startswith("WaveController"):
        return logging.getLogger(name)
    return logging.getLogger(f"WaveController.{name}")

def get_log_file_path() -> str:
    """Returns the absolute path to the main active log file."""
    return LOG_FILE

def get_log_dir_path() -> str:
    """Returns the absolute path to the logs folder."""
    return LOG_DIR

def get_log_size_str() -> str:
    """Returns human-readable size of the current log file."""
    if not os.path.exists(LOG_FILE):
        return "0 KB"
    try:
        size_bytes = os.path.getsize(LOG_FILE)
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
    except Exception:
        return "Unknown"

def export_logs_to(dest_path: str) -> bool:
    """Exports active log and any rotated logs to a destination file path."""
    try:
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"[{datetime.now()}] WaveController log file initialized.\n")
        
        # Flush all handlers
        root_logger = logging.getLogger("WaveController")
        for h in root_logger.handlers:
            h.flush()

        shutil.copy2(LOG_FILE, dest_path)
        root_logger.info(f"Successfully exported log file to {dest_path}")
        return True
    except Exception as e:
        logging.getLogger("WaveController").error(f"Failed to export log file: {e}")
        return False

def clear_logs() -> bool:
    """Clears current active log file contents."""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] [INFO] [WaveController] Log file cleared by user.\n")
        return True
    except Exception as e:
        logging.getLogger("WaveController").error(f"Failed to clear log file: {e}")
        return False
