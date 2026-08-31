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
    # Discord & Community Clients (Native, Flatpak, Snap, WebCord, Vesktop)
    "discord": ("Discord", "discord"),
    "discordcanary": ("Discord Canary", "discord"),
    "discordptb": ("Discord PTB", "discord"),
    "com.discordapp.discord": ("Discord", "discord"),
    "com.discordapp.discordcanary": ("Discord Canary", "discord"),
    "com.discordapp.discordptb": ("Discord PTB", "discord"),
    "vesktop": ("Discord", "discord"),
    "dev.vencord.vesktop": ("Discord", "discord"),
    "webcord": ("Discord", "discord"),
    "io.github.spacingbat3.webcord": ("Discord", "discord"),

    # Spotify & Music Players
    "spotify": ("Spotify", "spotify"),
    "com.spotify.client": ("Spotify", "spotify"),
    "cider": ("Cider", "cider"),
    "sh.cider.cider": ("Cider", "cider"),
    "strawberry": ("Strawberry", "strawberry"),
    "org.strawberrymusicplayer.strawberry": ("Strawberry", "strawberry"),
    "rhythmbox": ("Rhythmbox", "rhythmbox"),
    "org.gnome.rhythmbox3": ("Rhythmbox", "rhythmbox"),
    "audacity": ("Audacity", "audacity"),
    "org.audacityteam.audacity": ("Audacity", "audacity"),
    "shortwave": ("Shortwave", "de.haeckerfelix.Shortwave"),
    "de.haeckerfelix.shortwave": ("Shortwave", "de.haeckerfelix.Shortwave"),

    # VoIP & Collaboration
    "slack": ("Slack", "slack"),
    "com.slack.slack": ("Slack", "slack"),
    "teams": ("Microsoft Teams", "teams"),
    "teams-for-linux": ("Microsoft Teams", "teams"),
    "com.microsoft.teams": ("Microsoft Teams", "teams"),
    "signal-desktop": ("Signal", "signal"),
    "signal": ("Signal", "signal"),
    "org.signal.signal": ("Signal", "signal"),
    "whatsapp-for-linux": ("WhatsApp", "whatsapp"),
    "com.github.eneshecan.whatsappforlinux": ("WhatsApp", "whatsapp"),
    "element-desktop": ("Element", "element"),
    "element": ("Element", "element"),
    "im.riot.riot": ("Element", "element"),
    "telegram-desktop": ("Telegram", "telegram"),
    "telegram": ("Telegram", "telegram"),
    "org.telegram.desktop": ("Telegram", "telegram"),
    "skypeforlinux": ("Skype", "skype"),
    "com.skype.client": ("Skype", "skype"),
    "zoom": ("Zoom", "zoom"),
    "us.zoom.zoom": ("Zoom", "zoom"),

    # Web Browsers
    "firefox": ("Firefox", "firefox"),
    "org.mozilla.firefox": ("Firefox", "firefox"),
    "chrome": ("Google Chrome", "google-chrome"),
    "google-chrome": ("Google Chrome", "google-chrome"),
    "com.google.chrome": ("Google Chrome", "google-chrome"),
    "chromium": ("Chromium", "chromium"),
    "org.chromium.chromium": ("Chromium", "chromium"),
    "brave": ("Brave", "brave-browser"),
    "com.brave.browser": ("Brave", "brave-browser"),
    "msedge": ("Microsoft Edge", "microsoft-edge"),
    "microsoft-edge": ("Microsoft Edge", "microsoft-edge"),

    # Gaming & Video / Broadcasting
    "steam": ("Steam", "steam"),
    "steamwebhelper": ("Steam", "steam"),
    "com.valvesoftware.steam": ("Steam", "steam"),
    "obs": ("OBS Studio", "obs"),
    "obs64": ("OBS Studio", "obs"),
    "com.obsproject.studio": ("OBS Studio", "obs"),
    "vlc": ("VLC Media Player", "vlc"),
    "org.videolan.vlc": ("VLC Media Player", "vlc"),
    "mpv": ("MPV", "mpv"),
    "io.mpv.mpv": ("MPV", "mpv")
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

    # 3. Known Electron & communication app aliases (WebRTC voice engines & specific backends)
    if any(k in raw for k in ("discord", "slack", "teams", "signal", "whatsapp", "element", "matrix")):
        tokens.update({"webrtc", "webrtc voiceengine", "webrtc_voiceengine"})
    if any(k in raw for k in ("steam", "steamwebhelper")):
        tokens.update({"steam", "steamwebhelper"})
    if any(k in raw for k in ("chrome", "google")):
        tokens.update({"chrome", "google-chrome", "google_chrome", "google chrome", "chromium", "google-chrome-stable", "google-chrome-beta", "google-chrome-unstable"})
    if "chromium" in raw:
        tokens.update({"chromium", "org.chromium.chromium", "chrome"})
    if "brave" in raw:
        tokens.update({"brave", "brave-browser", "brave_browser", "chrome"})
    if "edge" in raw or "msedge" in raw:
        tokens.update({"msedge", "microsoft-edge", "edge", "chrome"})

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

    # Strip square brackets, parentheses, stream qualifiers, and dynamic numeric suffixes
    # e.g. "Google Chrome [Playback]", "Google Chrome-1", "Google Chrome (2) [Playback]" -> "google chrome"
    text_clean = re.sub(r'\[.*?\]|\(.*?\)', ' ', text_clean)
    words = [w for w in re.split(r'[\s\-_.:/]+', text_clean) if w]
    if t_clean in words:
        return True

    text_words_clean = " ".join(words)
    t_phrase = " ".join([w for w in re.split(r'[\s\-_.:/]+', t_clean) if w])
    if t_phrase and (t_phrase == text_words_clean or t_phrase in text_words_clean or text_words_clean in t_phrase):
        return True

    # Strip trailing numeric suffix e.g. "chrome-1", "google-chrome-2" -> "chrome"
    clean_no_num = re.sub(r'[\-_]?\d+$', '', text_words_clean).strip()
    if clean_no_num and (t_phrase == clean_no_num or clean_no_num in t_phrase or t_phrase in clean_no_num):
        return True

    return False

def port_matches_tokens(port_name: str, tokens: set, port_meta: dict = None) -> bool:
    """Checks if a PipeWire port belongs to an application or device matching any token."""
    if not port_name or not tokens:
        return False

    # Strip numeric port ID prefix if present from pw-link -I (e.g. "189 Google Chrome:output_FL" -> "Google Chrome:output_FL")
    clean_port = re.sub(r'^\d+\s+', '', port_name.strip())

    # Ignore internal submix loops, meters, and virtual adapters
    if clean_port.startswith("output.WaveController_") or clean_port.startswith("WaveController_") or clean_port.startswith("wave_"):
        return False

    p_low = clean_port.lower()
    node_part = p_low.split(":")[0]

    # 1. Primary Priority: Authoritative process binary metadata (prevents Electron / Chromium collisions)
    if port_meta:
        meta = port_meta.get(clean_port) or port_meta.get(p_low) or port_meta.get(port_name)
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

