"""
Process Classifier & Metadata Engine
====================================
Maps running processes, PipeWire streams, and port nodes to identifiable applications.
Ensures authoritative process binary disambiguation (Discord, Slack, Teams, Chrome).
"""

import re
import json
import subprocess

KNOWN_AUDIO_BINARIES = {
    "spotify": ("Spotify", "spotify"),
    "discord": ("Discord", "discord"),
    "discordcanary": ("Discord Canary", "discord"),
    "discordptb": ("Discord PTB", "discord"),
    "slack": ("Slack", "slack"),
    "teams": ("Microsoft Teams", "teams"),
    "teams-for-linux": ("Microsoft Teams", "teams"),
    "signal-desktop": ("Signal", "signal"),
    "signal": ("Signal", "signal"),
    "whatsapp-for-linux": ("WhatsApp", "whatsapp"),
    "element-desktop": ("Element", "element"),
    "steam": ("Steam", "steam"),
    "steamwebhelper": ("Steam", "steam"),
    "firefox": ("Firefox", "firefox"),
    "chrome": ("Google Chrome", "google-chrome"),
    "google-chrome": ("Google Chrome", "google-chrome"),
    "chromium": ("Chromium", "chromium"),
    "brave": ("Brave", "brave-browser"),
    "vlc": ("VLC Media Player", "vlc"),
    "mpv": ("MPV", "mpv"),
    "rhythmbox": ("Rhythmbox", "rhythmbox"),
    "audacity": ("Audacity", "audacity"),
    "obs": ("OBS Studio", "obs"),
    "obs64": ("OBS Studio", "obs"),
    "cider": ("Cider", "cider"),
    "strawberry": ("Strawberry", "strawberry"),
    "telegram-desktop": ("Telegram", "telegram"),
    "telegram": ("Telegram", "telegram"),
    "skypeforlinux": ("Skype", "skype"),
    "zoom": ("Zoom", "zoom"),
    "shortwave": ("Shortwave", "de.haeckerfelix.Shortwave"),
    "de.haeckerfelix.shortwave": ("Shortwave", "de.haeckerfelix.Shortwave")
}

def get_match_tokens(name_or_id: str) -> set:
    """Generates normalized matching tokens for any application, process binary, or audio device."""
    if not name_or_id:
        return set()
    raw = str(name_or_id).lower().strip()
    tokens = {raw}

    # 1. Spacing and punctuation permutations
    tokens.add(raw.replace(" ", "-"))
    tokens.add(raw.replace(" ", "_"))
    tokens.add(raw.replace(" ", ""))
    tokens.add(raw.replace("-", " "))
    tokens.add(raw.replace("_", " "))
    tokens.add(raw.replace("-", ""))
    tokens.add(raw.replace("_", ""))

    # 2. Known audio binary mappings (Chrome, VLC, Discord, Steam, OBS, Shortwave, etc.)
    for bin_name, (disp, alt) in KNOWN_AUDIO_BINARIES.items():
        if bin_name in raw or disp.lower() in raw or alt.lower() in raw:
            tokens.add(bin_name)
            tokens.add(alt.lower())
            tokens.add(disp.lower())
            tokens.add(disp.lower().replace(" ", "-"))
            tokens.add(disp.lower().replace(" ", "_"))

    # 3. Known Electron & communication app aliases (WebRTC voice engines & Chromium backends)
    if any(k in raw for k in ("discord", "slack", "teams", "signal", "whatsapp", "element", "matrix")):
        tokens.update({"webrtc", "webrtc voiceengine", "webrtc_voiceengine", "chromium"})
    if any(k in raw for k in ("steam", "steamwebhelper")):
        tokens.update({"steam", "steamwebhelper", "chromium"})
    if any(k in raw for k in ("chrome", "google")):
        tokens.update({"chrome", "google-chrome", "google_chrome", "chromium"})

    # 4. Known hardware device aliases
    if raw in ("elgato", "elgato wave xlr", "wave xlr", "wave:3", "wave:1", "wave neo") or raw.startswith("elgato_wave") or "wave_xlr" in raw or "wave:3" in raw or "wave:1" in raw or "wave_neo" in raw or "0fd9" in raw:
        tokens.update({"wave", "elgato", "0fd9", "wave_xlr", "wave-xlr"})
    if any(w in raw for w in ("fefine", "fifine", "3142")):
        tokens.update({"fifine", "fefine", "3142"})

    # 5. Extract individual distinct alphanumeric words (len >= 3)
    stop_words = {
        "the", "and", "for", "with", "player", "media", "audio", "sound",
        "stream", "desktop", "client", "app", "application", "input", "output",
        "stereo", "mono", "analog", "default", "system", "capture", "playback",
        "usb", "alsa", "pci", "card", "sink", "source", "device", "devices",
        "node", "nodes", "port", "ports"
    }
    words = [w for w in re.split(r"[\s\-_.:/]+", raw) if len(w) >= 3 and w not in stop_words]
    tokens.update(words)
    return tokens

def get_active_port_metadata_map() -> dict:
    """Extracts live process binary, application name, and node information for all PipeWire ports."""
    port_map = {}
    try:
        out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
        data = json.loads(out)
        nodes = {obj["id"]: obj for obj in data if obj.get("type") == "PipeWire:Interface:Node"}
        ports = [obj for obj in data if obj.get("type") == "PipeWire:Interface:Port"]
        for p in ports:
            props = p.get("info", {}).get("props", {})
            p_name = props.get("port.name", "")
            p_alias = props.get("port.alias", "")
            node_id = props.get("node.id")
            n_obj = nodes.get(node_id, {})
            n_props = n_obj.get("info", {}).get("props", {})
            n_name = n_props.get("node.name", "")
            app_name = n_props.get("application.name", "")
            app_bin = n_props.get("application.process.binary", "")
            app_id = n_props.get("application.id", "")
            
            meta = {
                "app_name": app_name,
                "binary": app_bin,
                "node_name": n_name,
                "app_id": app_id
            }
            if p_alias:
                port_map[p_alias] = meta
                port_map[p_alias.lower()] = meta
            if n_name and p_name:
                port_map[f"{n_name}:{p_name}"] = meta
                port_map[f"{n_name}:{p_name}".lower()] = meta
    except Exception:
        pass
    return port_map

def _word_match(token: str, text: str) -> bool:
    if not token or not text:
        return False
    t_clean = token.strip().lower()
    text_clean = text.strip().lower()
    if t_clean == text_clean:
        return True
    words = text_clean.replace("-", " ").replace("_", " ").replace(".", " ").split()
    if t_clean in words:
        return True
    if " " in t_clean and t_clean in text_clean:
        return True
    return False

def port_matches_tokens(port_name: str, tokens: set, port_meta: dict = None) -> bool:
    """Checks if a PipeWire port belongs to an application or device matching any token."""
    if not port_name or not tokens:
        return False

    # Ignore internal submix loops, meters, and virtual adapters
    if port_name.startswith("output.WaveController_") or port_name.startswith("WaveController_") or port_name.startswith("wave_"):
        return False

    p_low = port_name.lower()
    node_part = p_low.split(":")[0]

    # 1. Primary Priority: Authoritative process binary metadata (prevents Electron / Chromium collisions)
    if port_meta:
        meta = port_meta.get(port_name) or port_meta.get(p_low)
        if meta:
            bin_raw = str(meta.get("binary", "")).strip()
            if bin_raw:
                bin_file = bin_raw.lower().split("/")[-1].split("\\")[-1]
                matches_binary = any(_word_match(t, bin_file) or _word_match(t, bin_raw) for t in tokens if len(t) >= 3)
                if matches_binary:
                    return True
                else:
                    return False

            app_raw = str(meta.get("app_name", "")).strip().lower()
            if app_raw and app_raw not in ("chromium", "playback", "webrtc voiceengine"):
                matches_app = any(_word_match(t, app_raw) for t in tokens if len(t) >= 3)
                if matches_app:
                    return True
                else:
                    return False

    # 2. Fallback Priority: Generic node string matching
    for t in tokens:
        if len(t) < 3:
            continue
        if _word_match(t, node_part):
            return True

    return False
