"""
Application Stream Tracker & WirePlumber Router
================================================
Specialized tracker for desktop media applications (Spotify, Chromium, Discord, Steam, games).
Authoritatively isolates multi-stream child processes and handles WirePlumber target bindings.
"""

import re
from wavecontroller.utils.logger import get_logger
from wavecontroller.utils.pipewire_utils import safe_pw_dump

log = get_logger("AppStreamTracker")

class AppStreamTracker:
    """
    Discovers, classifies, and routes client application audio streams into WaveController channel strips.
    """
    def __init__(self, pipewire_mgr=None):
        self.pipewire_mgr = pipewire_mgr
        self._bound_stream_nodes = set()

    def get_detected_apps(self) -> list:
        """Returns sorted list of identifiable running desktop audio application names."""
        apps = set()
        for stream in self.get_active_application_streams():
            name = stream.get("name")
            if name and not name.startswith("WaveController") and not name.startswith("pw-loopback"):
                apps.add(name)
        return sorted(list(apps))

    def get_active_application_streams(self) -> list:
        """Extracts all currently active playback audio stream nodes from PipeWire."""
        streams = []
        try:
            data = safe_pw_dump(timeout=3)
            clients = {
                obj["id"]: (obj.get("info") or {}).get("props") or {}
                for obj in data
                if isinstance(obj, dict) and obj.get("type") == "PipeWire:Interface:Client"
            }
            for obj in data:
                if isinstance(obj, dict) and obj.get("type") == "PipeWire:Interface:Node":
                    props = (obj.get("info") or {}).get("props") or {}
                    media_class = props.get("media.class", "")
                    media_role = props.get("media.role", "")
                    if media_class == "Stream/Output/Audio" and media_role != "DSP":
                        c_id = props.get("client.id")
                        c_props = clients.get(c_id, {}) if c_id is not None else {}
                        app_name = props.get("application.name") or props.get("node.name") or "Unknown App"

                        # Unwrap PipeWire ALSA and ALSA playback wrapper
                        m_alsa = re.match(r"^(?:PipeWire\s+ALSA|ALSA\s+Playback)\s*\[(.*?)\]", app_name, re.IGNORECASE)
                        if m_alsa:
                            unwrapped = m_alsa.group(1).strip()
                            if unwrapped:
                                app_name = unwrapped

                        node_name = props.get("node.name", "")
                        if app_name.lower() in ("alsa playback", "pipewire alsa") and node_name.startswith("alsa_playback."):
                            extracted = node_name[len("alsa_playback."):].strip()
                            if extracted:
                                app_name = extracted

                        name_stripped = re.sub(r"\s*\[Playback\]\s*$", "", app_name, flags=re.IGNORECASE).strip()
                        if name_stripped:
                            app_name = name_stripped

                        name_low = str(app_name).lower()
                        if any(kw in name_low for kw in ("wavecontroller", "pw-loopback", "wave_meter_", "libremidi", "midi-bridge", "bluez_midi", "pavucontrol")):
                            continue
                        app_bin = props.get("application.process.binary", "") or c_props.get("application.process.binary", "")
                        node_id = obj.get("id")
                        streams.append({
                            "id": node_id,
                            "name": app_name,
                            "binary": app_bin,
                            "media_name": props.get("media.name", ""),
                            "props": props
                        })
        except Exception:
            pass
        return streams
