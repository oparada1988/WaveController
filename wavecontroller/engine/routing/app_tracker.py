"""
Application Stream Tracker & WirePlumber Router
================================================
Specialized tracker for desktop media applications (Spotify, Chromium, Discord, Steam, games).
Authoritatively isolates multi-stream child processes and handles WirePlumber target bindings.
"""

import re
import json
import subprocess
from wavecontroller.engine.graph.process_classifier import KNOWN_AUDIO_BINARIES, get_match_tokens, get_active_port_metadata_map, port_matches_tokens
from wavecontroller.engine.graph.stream_resolver import get_multi_stream_numeric_ports
from wavecontroller.utils.logger import get_logger

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
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    media_class = props.get("media.class", "")
                    media_role = props.get("media.role", "")
                    if media_class == "Stream/Output/Audio" and media_role != "DSP":
                        app_name = props.get("application.name") or props.get("node.name") or "Unknown App"
                        name_low = str(app_name).lower()
                        if any(kw in name_low for kw in ("wavecontroller", "pw-loopback", "wave_meter_", "libremidi", "midi-bridge", "bluez_midi", "pavucontrol")):
                            continue
                        app_bin = props.get("application.process.binary", "")
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

    def get_app_out_ports(self, assigned_apps: list, out_ports: list, port_meta: dict = None) -> list:
        """
        Returns exact numeric output ports for all assigned applications belonging to a channel strip.
        """
        app_out_ports = []
        for app in assigned_apps:
            tokens = get_match_tokens(app)
            multi_ports = get_multi_stream_numeric_ports(tokens, port_meta=port_meta, out_ports=out_ports)
            for item in multi_ports:
                app_out_ports.append(item["port_id"])
                
            for p in out_ports:
                clean_p = re.sub(r"^\d+\s+", "", p).strip()
                if clean_p.startswith("output.WaveController_") or clean_p.startswith("WaveController_") or clean_p.startswith("wave_"):
                    continue
                if ":output_" in clean_p and port_matches_tokens(clean_p, tokens, port_meta):
                    p_id = p.split()[0] if p and p.split()[0].isdigit() else clean_p
                    if p_id not in app_out_ports:
                        app_out_ports.append(p_id)
        return app_out_ports

    def bind_app_to_target_sink(self, app_name: str, channel_id: str):
        """Directs WirePlumber metadata to route an application into its dedicated channel sink."""
        try:
            target_sink = f"WaveController_Channel_{channel_id}" if not channel_id.startswith("WaveController_") else channel_id
            tokens = get_match_tokens(app_name)
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            for obj in json.loads(out):
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    if props.get("media.class") == "Stream/Output/Audio":
                        if any(t in str(props.get("application.name", "")).lower() or t in str(props.get("application.process.binary", "")).lower() for t in tokens if len(t) >= 3):
                            node_id = str(obj["id"])
                            subprocess.run(["wpctl", "set-sink", node_id, target_sink], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            self._bound_stream_nodes.add(node_id)
        except Exception:
            pass

    def unbind_app_from_target_sink(self, app_name: str):
        """Releases application stream back to default system sink."""
        try:
            tokens = get_match_tokens(app_name)
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            for obj in json.loads(out):
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    if props.get("media.class") == "Stream/Output/Audio":
                        if any(t in str(props.get("application.name", "")).lower() or t in str(props.get("application.process.binary", "")).lower() for t in tokens if len(t) >= 3):
                            node_id = str(obj["id"])
                            subprocess.run(["wpctl", "set-sink", node_id, "WaveController_personal_Sink"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            self._bound_stream_nodes.discard(node_id)
        except Exception:
            pass
