"""
Process Classifier & Metadata Engine
====================================
Maps running processes, PipeWire streams, and port nodes to identifiable applications.
Ensures authoritative process binary disambiguation (Discord, Slack, Teams, Chrome).
"""

import os
import re
import time
import threading
from wavecontroller.utils.pipewire_utils import safe_pw_dump, invalidate_pw_dump_cache

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
    "feishin": ("Feishin", "feishin"),
    "org.jeffvli.feishin": ("Feishin", "feishin"),
    "music-assistant-companion": ("Music Assistant", "music-assistant-companion"),
    "music-assistant": ("Music Assistant", "music-assistant-companion"),
    "org.music_assistant.companion": ("Music Assistant", "music-assistant-companion"),
    "plexamp": ("Plexamp", "plexamp"),
    "com.plexamp.plexamp": ("Plexamp", "plexamp"),
    "sublime-music": ("Sublime Music", "sublime-music"),
    "tauonmb": ("Tauon Music Box", "tauonmb"),
    "g4music": ("G4Music", "g4music"),
    "lollypop": ("Lollypop", "org.gnome.Lollypop"),
    "amberol": ("Amberol", "io.bassi.Amberol"),
    "elisa": ("Elisa", "org.kde.elisa"),
    "cantata": ("Cantata", "cantata"),
    "deadbeef": ("DeaDBeeF", "deadbeef"),
    "clementine": ("Clementine", "clementine"),
    "jellyfinmediaplayer": ("Jellyfin Media Player", "jellyfinmediaplayer"),
    "com.github.iwalton3.jellyfin-media-player": ("Jellyfin Media Player", "jellyfinmediaplayer"),
    "tidal-hifi": ("TIDAL Hi-Fi", "tidal-hifi"),

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

GENERIC_COMPATIBILITY_RUNNERS = {
    "wine64-preloader", "wine-preloader", "wineserver", "wine", "wine64",
    "pressure-vessel-adverb", "pressure-vessel", "pv-adverb", "srt-bwrap",
    "gamescope", "reaper", "steam", "steamwebhelper", "proton", "apprun",
    "launcher", "wrapper", "run.sh", "start.sh", "launch.sh", "exe"
}

DEDICATED_NON_GAME_BINARIES = frozenset(
    k for k in KNOWN_AUDIO_BINARIES.keys()
    if k not in ("steam", "steamwebhelper", "com.valvesoftware.steam")
)

_DYNAMIC_DESKTOP_REGISTRY = {}
_DYNAMIC_DESKTOP_REGISTRY_TS = 0.0
_DYNAMIC_DESKTOP_LOCK = threading.Lock()

def get_dynamic_audio_desktop_registry(max_age_s: float = 30.0) -> dict:
    """
    Dynamically scans XDG .desktop application files to discover installed
    audio/video players, DAWs, video editors, and voice/streaming apps.
    Caches results with a 30-second TTL to avoid redundant disk I/O.
    """
    global _DYNAMIC_DESKTOP_REGISTRY, _DYNAMIC_DESKTOP_REGISTRY_TS
    now = time.monotonic()
    with _DYNAMIC_DESKTOP_LOCK:
        if (now - _DYNAMIC_DESKTOP_REGISTRY_TS) < max_age_s and _DYNAMIC_DESKTOP_REGISTRY:
            return dict(_DYNAMIC_DESKTOP_REGISTRY)

    dirs = [
        "/var/lib/flatpak/exports/share/applications",
        os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
        "/usr/share/applications",
        os.path.expanduser("~/.local/share/applications"),
        "/usr/local/share/applications"
    ]
    AUDIO_KEYWORDS = {
        "audio", "audiovideo", "player", "music", "recorder", "audiovideoediting",
        "midi", "sequencer", "sound", "synth", "daw", "browser", "chat", "telephony", "video"
    }
    UTILITY_EXCLUSIONS = {
        "wavecontroller", "com.oparada.wavecontroller",
        "pavucontrol", "org.pulseaudio.pavucontrol", "volume control",
        "qpwgraph", "org.rncbc.qpwgraph", "helvum", "patchance",
        "easyeffects", "com.github.wwmm.easyeffects",
        "pw-loopback", "wireplumber", "pipewire"
    }
    registry = {}
    import shlex
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            for f in os.listdir(d):
                if not f.endswith(".desktop"):
                    continue
                path = os.path.join(d, f)
                try:
                    name, exec_cmd, icon, cats, comment = "", "", "", set(), ""
                    with open(path, "r", encoding="utf-8", errors="ignore") as fp:
                        for line in fp:
                            line = line.strip()
                            if line.startswith("Name=") and not name:
                                name = line.split("=", 1)[1].strip()
                            elif line.startswith("Exec=") and not exec_cmd:
                                exec_cmd = line.split("=", 1)[1].strip()
                            elif line.startswith("Icon=") and not icon:
                                icon = line.split("=", 1)[1].strip()
                            elif line.startswith("Comment=") and not comment:
                                comment = line.split("=", 1)[1].strip()
                            elif line.startswith("Categories="):
                                c_list = line.split("=", 1)[1].lower().split(";")
                                cats = {c.strip() for c in c_list if c.strip()}
                            elif line.startswith("NoDisplay=true"):
                                name = ""
                                break
                    if not name or not exec_cmd or name.lower() in UTILITY_EXCLUSIONS or f[:-8].lower() in UTILITY_EXCLUSIONS:
                        continue
                    is_audio = bool(cats & AUDIO_KEYWORDS)
                    if not is_audio:
                        search_text = f"{name} {comment} {exec_cmd}".lower()
                        if any(kw in search_text for kw in ("music", "audio", "player", "sound", "podcast", "daw", "synth", "sequencer", "video editor")):
                            is_audio = True
                    if is_audio:
                        entry = (name, icon or name.lower())
                        app_id = f[:-8].lower()
                        registry[app_id] = entry
                        try:
                            parts = shlex.split(exec_cmd)
                        except Exception:
                            parts = exec_cmd.split()
                        if parts:
                            idx = 0
                            if parts[idx] == "env":
                                idx += 1
                                while idx < len(parts) and "=" in parts[idx] and not parts[idx].startswith("-"):
                                    idx += 1
                            if idx < len(parts):
                                target = parts[idx]
                                if target.endswith(("flatpak", "flatpak-spawn")):
                                    for p in parts[idx+1:]:
                                        if p.startswith("--command="):
                                            c_cmd = p.split("=", 1)[1].lower().strip()
                                            registry[c_cmd] = entry
                                        if "." in p and not p.startswith("-"):
                                            registry[p.lower()] = entry
                                            registry[p.split(".")[-1].lower()] = entry
                                else:
                                    bin_base = target.split("/")[-1].split("\\")[-1].lower()
                                    registry[bin_base] = entry
                except Exception:
                    pass
        except Exception:
            pass

    with _DYNAMIC_DESKTOP_LOCK:
        _DYNAMIC_DESKTOP_REGISTRY = registry
        _DYNAMIC_DESKTOP_REGISTRY_TS = now
    return registry

def get_known_audio_applications() -> dict:
    """
    Returns an aggregated dictionary of all known and dynamically discovered desktop audio applications.
    Combines the baseline KNOWN_AUDIO_BINARIES safety-net dictionary with dynamic XDG .desktop discovery.
    """
    dynamic_apps = get_dynamic_audio_desktop_registry()
    combined = dict(KNOWN_AUDIO_BINARIES)
    combined.update(dynamic_apps)
    return combined

_WINE_SYSTEM_EXES = frozenset({
    "services.exe", "winedevice.exe", "plugplay.exe", "svchost.exe",
    "rpcss.exe", "explorer.exe", "conhost.exe", "tabtip.exe",
    "steam.exe", "steamwebhelper.exe", "crashreportclient.exe",
    "unitycrashhandler64.exe", "unitycrashhandler32.exe"
})

_PID_GAME_META_CACHE = {}
_PID_GAME_META_LOCK = threading.Lock()


def resolve_pid_game_metadata(pid) -> dict:
    """Extracts Steam game title/folder, .exe binary, and SteamAppId from /proc/<pid> (cached per PID)."""
    if not pid:
        return {}
    try:
        pid_int = int(pid)
    except (ValueError, TypeError):
        return {}
    if pid_int <= 0:
        return {}

    with _PID_GAME_META_LOCK:
        if pid_int in _PID_GAME_META_CACHE:
            return _PID_GAME_META_CACHE[pid_int]

    meta = {"steam_game_name": "", "exe_name": "", "steam_app_id": ""}
    curr_pid = pid_int

    for _ in range(4):
        if curr_pid <= 1 or not os.path.exists(f"/proc/{curr_pid}"):
            break
        try:
            # 1. Inspect cmdline for .exe and /steamapps/common/<GameName>
            cmdline_path = f"/proc/{curr_pid}/cmdline"
            if os.path.exists(cmdline_path):
                with open(cmdline_path, "rb") as f:
                    raw_args = [a.decode("utf-8", errors="ignore").strip() for a in f.read().split(b"\x00") if a]
                for arg in raw_args:
                    arg_norm = arg.replace("\\", "/")
                    if not meta["steam_game_name"] and "steamapps/common/" in arg_norm.lower():
                        m = re.search(r"steamapps/common/([^/]+)", arg_norm, re.IGNORECASE)
                        if m:
                            folder = m.group(1).strip()
                            if not folder.lower().startswith(("proton", "steamlinuxruntime")):
                                meta["steam_game_name"] = folder
                    if not meta["exe_name"] and arg_norm.lower().endswith(".exe"):
                        exe_base = arg_norm.split("/")[-1].strip()
                        if exe_base.lower() not in _WINE_SYSTEM_EXES:
                            meta["exe_name"] = exe_base

            # 2. Inspect cwd symlink for /steamapps/common/<GameName>
            if not meta["steam_game_name"]:
                try:
                    cwd_target = os.readlink(f"/proc/{curr_pid}/cwd").replace("\\", "/")
                    if "steamapps/common/" in cwd_target.lower():
                        m = re.search(r"steamapps/common/([^/]+)", cwd_target, re.IGNORECASE)
                        if m:
                            folder = m.group(1).strip()
                            if not folder.lower().startswith(("proton", "steamlinuxruntime")):
                                meta["steam_game_name"] = folder
                except Exception:
                    pass

            # 3. Inspect environ for STEAM_COMPAT_INSTALL_PATH and SteamAppId
            if not meta["steam_game_name"] or not meta["steam_app_id"]:
                env_path = f"/proc/{curr_pid}/environ"
                if os.path.exists(env_path):
                    with open(env_path, "rb") as f:
                        env_entries = f.read().split(b"\x00")
                    for entry in env_entries:
                        if entry.startswith(b"STEAM_COMPAT_INSTALL_PATH=") and not meta["steam_game_name"]:
                            val = entry.split(b"=", 1)[1].decode("utf-8", errors="ignore").replace("\\", "/").strip()
                            folder = val.rstrip("/").split("/")[-1].strip()
                            if folder and not folder.lower().startswith(("proton", "steamlinuxruntime")):
                                meta["steam_game_name"] = folder
                        elif entry.startswith((b"SteamAppId=", b"SteamGameId=")) and not meta["steam_app_id"]:
                            val = entry.split(b"=", 1)[1].decode("utf-8", errors="ignore").strip()
                            if val and val != "0":
                                meta["steam_app_id"] = val

            if meta["steam_game_name"] and meta["exe_name"]:
                break

            # Walk up to parent PID via /proc/<pid>/stat
            stat_path = f"/proc/{curr_pid}/stat"
            if os.path.exists(stat_path):
                with open(stat_path, "r", encoding="utf-8", errors="ignore") as f:
                    stat_str = f.read()
                rparen = stat_str.rfind(")")
                if rparen != -1:
                    fields = stat_str[rparen + 2:].split()
                    if len(fields) >= 2 and fields[1].isdigit():
                        curr_pid = int(fields[1])
                        continue
            break
        except Exception:
            break

    with _PID_GAME_META_LOCK:
        if len(_PID_GAME_META_CACHE) > 512:
            _PID_GAME_META_CACHE.clear()
        _PID_GAME_META_CACHE[pid_int] = meta

    return meta


def get_match_tokens(name_or_id: str) -> set:
    """Generates normalized matching tokens for any application, process binary, or audio device."""
    if not name_or_id:
        return set()
    raw_orig = str(name_or_id).strip()
    # Split CamelCase words (e.g. "HaloCampaignEvolved.exe" -> "Halo Campaign Evolved.exe")
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw_orig).lower().strip()
    raw = raw_orig.lower()
    tokens = {raw}
    if camel_split != raw:
        tokens.add(camel_split)

    # Strip common executable extensions (.exe, .x86_64, .bin)
    for ext in (".exe", ".x86_64", ".x86", ".bin", ".sh"):
        if raw.endswith(ext) and len(raw) > len(ext) + 2:
            stem = raw[:-len(ext)].strip()
            tokens.add(stem)
            camel_stem = camel_split[:-len(ext)].strip() if camel_split.endswith(ext) else camel_split
            if camel_stem:
                tokens.add(camel_stem)

    # 1. Spacing, colon, and punctuation permutations
    no_punct = re.sub(r"[:'\"!?,]+", "", raw).strip()
    no_punct = re.sub(r"\s+", " ", no_punct)
    for variant in {raw, no_punct, camel_split}:
        if not variant:
            continue
        tokens.add(variant)
        tokens.add(variant.replace(" ", "-"))
        tokens.add(variant.replace(" ", "_"))
        tokens.add(variant.replace(" ", ""))
        tokens.add(variant.replace("-", " "))
        tokens.add(variant.replace("_", " "))
        tokens.add(variant.replace("-", ""))
        tokens.add(variant.replace("_", ""))
        alnum_compact = re.sub(r"[^a-z0-9]+", "", variant)
        if len(alnum_compact) >= 3:
            tokens.add(alnum_compact)

    # 2. Known audio binary mappings (Chrome, VLC, Discord, Steam, OBS, Shortwave, dynamic desktop apps, etc.)
    all_known_apps = get_known_audio_applications()
    for bin_name, (disp, alt) in all_known_apps.items():
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
    # NOTE: "mic"/"microphone" are intentionally excluded here even though they are not truly
    # generic English filler words. Channel ids/names like the default "Microphone" channel would
    # otherwise tokenize to "microphone" and fuzzy-match ANY other connected capture device whose
    # description also contains that word (e.g. "fifine Microphone Analog Stereo"), silently
    # routing a second physical device's audio into the first device's channel/submix. Matching
    # must instead rely on the specific device name/key the user actually assigned.
    stop_words = {
        "the", "and", "for", "with", "player", "media", "audio", "sound",
        "stream", "desktop", "client", "app", "application", "input", "output",
        "stereo", "mono", "analog", "default", "system", "capture", "playback",
        "usb", "alsa", "pci", "card", "sink", "source", "device", "devices",
        "node", "nodes", "port", "ports", "mic", "mics", "microphone", "microphones",
        "exe", "win64", "shipping", "x64", "x86", "bin", "linux",
        "com", "org", "net", "io", "apps", "dev", "sys", "usr", "lib", "var", "etc",
        "gitlab", "github", "package", "service", "daemon", "helper"
    }
    for src_str in (raw, camel_split):
        words = [w for w in re.split(r"[\s\-_.:/]+", src_str) if len(w) >= 3 and w not in stop_words]
        tokens.update(words)
    return tokens

_PORT_METADATA_CACHE = {}
_PORT_METADATA_CACHE_TIME = 0.0
_PORT_METADATA_LOCK = threading.Lock()

def invalidate_port_metadata_cache():
    """Forces immediate invalidation of cached PipeWire port metadata."""
    global _PORT_METADATA_CACHE_TIME
    invalidate_pw_dump_cache()
    with _PORT_METADATA_LOCK:
        _PORT_METADATA_CACHE_TIME = 0.0

def get_active_port_metadata_map(force_refresh: bool = False, max_age_s: float = 0.8) -> dict:
    """Extracts live process binary, application name, and node information for all PipeWire ports.
    Employs a short thread-safe TTL cache (default 0.8s) to eliminate redundant pw-dump forks."""
    global _PORT_METADATA_CACHE, _PORT_METADATA_CACHE_TIME
    now = time.time()
    with _PORT_METADATA_LOCK:
        if not force_refresh and (now - _PORT_METADATA_CACHE_TIME) < max_age_s and _PORT_METADATA_CACHE:
            return dict(_PORT_METADATA_CACHE)

    port_map = {}
    try:
        data = safe_pw_dump(timeout=3)
        nodes = {obj["id"]: obj for obj in data if isinstance(obj, dict) and obj.get("type") == "PipeWire:Interface:Node"}
        ports = [obj for obj in data if isinstance(obj, dict) and obj.get("type") == "PipeWire:Interface:Port"]
        clients = {
            obj["id"]: (obj.get("info") or {}).get("props") or {}
            for obj in data
            if isinstance(obj, dict) and obj.get("type") == "PipeWire:Interface:Client"
        }
        for p in ports:
            p_id = p.get("id")
            props = (p.get("info") or {}).get("props") or {}
            p_name = props.get("port.name", "")
            p_alias = props.get("port.alias", "")
            node_id = props.get("node.id")
            n_obj = nodes.get(node_id, {})
            n_props = (n_obj.get("info") or {}).get("props") or {}
            c_id = n_props.get("client.id")
            c_props = clients.get(c_id, {}) if c_id is not None else {}

            n_name = n_props.get("node.name", "")
            n_desc = n_props.get("node.description", "")
            media_name = n_props.get("media.name", "")
            app_name = n_props.get("application.name", "") or c_props.get("application.name", "")
            app_bin = n_props.get("application.process.binary", "") or c_props.get("application.process.binary", "")
            app_id = (
                n_props.get("application.id", "")
                or n_props.get("pipewire.access.portal.app_id", "")
                or c_props.get("application.id", "")
                or c_props.get("pipewire.access.portal.app_id", "")
            )
            pid = (
                n_props.get("application.process.id")
                or n_props.get("pipewire.sec.pid")
                or c_props.get("application.process.id")
                or c_props.get("pipewire.sec.pid")
            )

            # Unwrap PipeWire ALSA and ALSA Playback stream wrappers
            if app_name:
                m_alsa = re.match(r"^(?:PipeWire\s+ALSA|ALSA\s+Playback)\s*\[(.*?)\]", app_name, re.IGNORECASE)
                if m_alsa:
                    unwrapped = m_alsa.group(1).strip()
                    if unwrapped and not app_bin:
                        app_bin = unwrapped
            if n_name.startswith("alsa_playback.") and not app_bin:
                app_bin = n_name[len("alsa_playback."):].strip()

            pid_meta = resolve_pid_game_metadata(pid) if pid else {}

            meta = {
                "port_id": p_id,
                "node_id": node_id,
                "app_name": app_name,
                "binary": app_bin,
                "node_name": n_name,
                "node_description": n_desc,
                "media_name": media_name,
                "app_id": app_id,
                "pid": pid,
                "steam_game_name": pid_meta.get("steam_game_name", ""),
                "exe_name": pid_meta.get("exe_name", ""),
                "steam_app_id": pid_meta.get("steam_app_id", "")
            }
            if p_id is not None:
                port_map[str(p_id)] = meta
                if n_name and p_name:
                    port_map[f"{p_id} {n_name}:{p_name}"] = meta
                    port_map[f"{p_id} {n_name}:{p_name}".lower()] = meta
            if p_alias:
                port_map[p_alias] = meta
                port_map[p_alias.lower()] = meta
            if n_name and p_name:
                port_map[f"{n_name}:{p_name}"] = meta
                port_map[f"{n_name}:{p_name}".lower()] = meta
    except Exception:
        pass

    with _PORT_METADATA_LOCK:
        if port_map:
            _PORT_METADATA_CACHE = port_map
            _PORT_METADATA_CACHE_TIME = now
        elif _PORT_METADATA_CACHE:
            return dict(_PORT_METADATA_CACHE)

    return port_map

def _word_match(token: str, text: str) -> bool:
    if not token or not text:
        return False
    t_clean = token.strip().lower()
    text_clean = text.strip().lower()
    if t_clean == text_clean:
        return True

    # Also compare compact alphanumeric forms for CamelCase / colon-separated game names
    t_alnum = re.sub(r'[^a-z0-9]+', '', t_clean)
    text_no_ext = re.sub(r'\.(exe|x86_64|x86|bin|sh)$', '', text_clean)
    text_alnum = re.sub(r'[^a-z0-9]+', '', text_no_ext)
    if len(t_alnum) >= 4 and len(text_alnum) >= 4:
        if t_alnum == text_alnum or (len(t_alnum) >= 5 and t_alnum in text_alnum) or (len(text_alnum) >= 5 and text_alnum in t_alnum):
            return True

    # Strip square brackets, parentheses, stream qualifiers, and dynamic numeric suffixes
    # e.g. "Google Chrome [Playback]", "Google Chrome-1", "Google Chrome (2) [Playback]" -> "google chrome"
    text_clean = re.sub(r'\[.*?\]|\(.*?\)', ' ', text_clean)
    words = [w for w in re.split(r'[\s\-_.:/]+', text_clean) if w]
    if t_clean in words:
        return True

    text_words_clean = " ".join(words)
    t_words = [w for w in re.split(r'[\s\-_.:/]+', t_clean) if w]
    if len(t_words) > 1:
        t_phrase = " ".join(t_words)
        if re.search(r'\b' + re.escape(t_phrase) + r'\b', text_words_clean):
            return True
        if re.search(r'\b' + re.escape(text_words_clean) + r'\b', t_phrase):
            return True

    # Strip trailing numeric suffix e.g. "chrome-1", "google-chrome-2" -> "chrome"
    clean_no_num = re.sub(r'[\-_]?\d+$', '', text_words_clean).strip()
    if clean_no_num and clean_no_num != text_words_clean:
        clean_words = clean_no_num.split()
        if t_clean in clean_words:
            return True
        if len(t_words) > 1:
            t_phrase = " ".join(t_words)
            if re.search(r'\b' + re.escape(t_phrase) + r'\b', clean_no_num):
                return True

    return False

def port_matches_tokens(port_name: str, tokens: set, port_meta: dict = None) -> bool:
    """Checks if a PipeWire port belongs to an application or device matching any token."""
    if not port_name or not tokens:
        return False

    raw_stripped = port_name.strip()
    port_id_str = raw_stripped.split()[0] if raw_stripped and raw_stripped.split()[0].isdigit() else None

    # Strip numeric port ID prefix if present from pw-link -I (e.g. "189 Google Chrome:output_FL" -> "Google Chrome:output_FL")
    clean_port = re.sub(r'^\d+\s+', '', raw_stripped)

    # Ignore internal submix loops, meters, virtual adapters, and non-audio V4L2/camera video ports
    if clean_port.startswith(("output.WaveController_", "input.WaveController_", "WaveController_", "wave_", "v4l2_", "libcamera_")):
        return False

    p_low = clean_port.lower()
    # Use rsplit(":", 1)[0] so application/node names containing colons (e.g. "Halo: Campaign Evolved:output_FL") are preserved intact
    node_part = p_low.rsplit(":", 1)[0] if ":" in p_low else p_low

    # 1. Primary Priority: Authoritative process & port metadata
    if port_meta:
        meta = (
            (port_meta.get(port_id_str) if port_id_str else None)
            or port_meta.get(raw_stripped)
            or port_meta.get(clean_port)
            or port_meta.get(p_low)
        )
        if meta:
            meta_node_name = str(meta.get("node_name", "")).strip()
            if meta_node_name.startswith(("output.WaveController_", "input.WaveController_", "WaveController_", "wave_", "v4l2_", "libcamera_")):
                return False
            bin_raw = str(meta.get("binary", "")).strip()
            exe_raw = str(meta.get("exe_name", "")).strip()
            steam_game = str(meta.get("steam_game_name", "")).strip()

            if bin_raw:
                bin_file = bin_raw.lower().split("/")[-1].split("\\")[-1]
                matches_binary = any(_word_match(t, bin_file) or _word_match(t, bin_raw) for t in tokens if len(t) >= 3)
                if matches_binary:
                    return True

            if exe_raw:
                if any(_word_match(t, exe_raw) for t in tokens if len(t) >= 3):
                    return True

            if steam_game:
                if any(_word_match(t, steam_game) for t in tokens if len(t) >= 3):
                    return True

            if bin_raw:
                bin_file = bin_raw.lower().split("/")[-1].split("\\")[-1]
                # Only abort early if this is a known dedicated desktop app (e.g. Discord, Spotify, Chrome, VLC)
                # that did not match the target tokens, preventing Electron/Chromium cross-app collisions
                # while allowing games and custom binaries whose executable name differs from their title.
                if bin_file in DEDICATED_NON_GAME_BINARIES and not steam_game and not exe_raw:
                    return False

            for field_key in ("app_name", "node_name", "node_description", "media_name", "app_id"):
                val_raw = str(meta.get(field_key, "")).strip().lower()
                if not val_raw:
                    continue
                if field_key == "app_name" and val_raw in ("chromium", "playback", "webrtc voiceengine", "webrtc_audio_sink"):
                    continue
                if any(_word_match(t, val_raw) for t in tokens if len(t) >= 3):
                    return True

    # 2. Fallback Priority: Generic node string matching
    for t in tokens:
        if len(t) < 3:
            continue
        if _word_match(t, node_part):
            return True

    return False


def score_port_match(port_name: str, channel_assigned: list, channel_id: str = "", port_meta: dict = None) -> int:
    """
    Calculates a match confidence score (0 to 100) between a PipeWire port and a channel's assigned apps/devices.
    Returns 0 if no match or if the port is an internal WaveController/video port.
    Higher scores indicate higher confidence/specificity:
      - 100: Exact match on binary filename, full binary path, or app_id
      - 80: Exact word/phrase match on steam_game_name, exe_name, or authoritative app_name
      - 70: Token match on binary filename or app_id
      - 60: Exact word match on node description, node name, or media name
      - 50: Token match on node name, node description, or port name string
      - 0: No match
    """
    if not port_name or not channel_assigned:
        return 0

    raw_stripped = port_name.strip()
    port_id_str = raw_stripped.split()[0] if raw_stripped and raw_stripped.split()[0].isdigit() else None
    clean_port = re.sub(r'^\d+\s+', '', raw_stripped)

    # Exclude internal loopbacks, meters, virtual nodes, video/v4l2 ports
    if clean_port.startswith(("output.WaveController_", "input.WaveController_", "WaveController_", "wave_", "v4l2_", "libcamera_")):
        return 0

    p_low = clean_port.lower()
    node_part = p_low.rsplit(":", 1)[0] if ":" in p_low else p_low

    meta = None
    if port_meta:
        meta = (
            (port_meta.get(port_id_str) if port_id_str else None)
            or port_meta.get(raw_stripped)
            or port_meta.get(clean_port)
            or port_meta.get(p_low)
        )

    assigned_cleaned = [str(a).strip().lower() for a in channel_assigned if a]
    assigned_tokens = set()
    for a in channel_assigned:
        assigned_tokens |= get_match_tokens(a)

    bin_raw = str(meta.get("binary", "")).strip().lower() if meta else ""
    bin_file = bin_raw.split("/")[-1].split("\\")[-1] if bin_raw else ""
    app_id = str(meta.get("app_id", "")).strip().lower() if meta else ""
    app_name = str(meta.get("app_name", "")).strip().lower() if meta else ""
    steam_game = str(meta.get("steam_game_name", "")).strip().lower() if meta else ""
    exe_name = str(meta.get("exe_name", "")).strip().lower() if meta else ""
    node_name = str(meta.get("node_name", "")).strip().lower() if meta else ""
    node_desc = str(meta.get("node_description", "")).strip().lower() if meta else ""
    media_name = str(meta.get("media_name", "")).strip().lower() if meta else ""

    # Priority 1: Exact binary or app_id matches (Score 100)
    for a in assigned_cleaned:
        if bin_file and a == bin_file:
            return 100
        if bin_raw and a == bin_raw:
            return 100
        if app_id and a == app_id:
            return 100

    # Priority 2: Authoritative metadata matches (Score 80)
    for a in assigned_cleaned:
        if steam_game and _word_match(a, steam_game):
            return 80
        if exe_name and _word_match(a, exe_name):
            return 80
        if app_name and app_name not in ("chromium", "playback", "webrtc voiceengine", "webrtc_audio_sink"):
            if _word_match(a, app_name):
                return 80

    # Priority 3: Token match on binary or app_id (Score 70)
    matched_p3 = False
    if bin_file:
        for t in assigned_tokens:
            if len(t) >= 3 and _word_match(t, bin_file):
                matched_p3 = True
                break
    if not matched_p3 and app_id:
        for t in assigned_tokens:
            if len(t) >= 3 and _word_match(t, app_id):
                matched_p3 = True
                break
    if matched_p3:
        return 70

    # If this is a known dedicated desktop app that failed binary/app_id/app_name matching,
    # prevent it from matching generic fallback node descriptions of unrelated channels.
    if bin_file in DEDICATED_NON_GAME_BINARIES and not steam_game and not exe_name:
        return 0

    # Priority 4: Exact word match on node description, node name, or media name (Score 60)
    for candidate in (node_desc, node_name, media_name):
        if candidate:
            for a in assigned_cleaned:
                if len(a) >= 3 and _word_match(a, candidate):
                    return 60

    # Priority 5: Fallback token match on node description, node name, or port string (Score 50)
    for candidate in (node_desc, node_name, node_part):
        if candidate:
            for t in assigned_tokens:
                if len(t) >= 3 and _word_match(t, candidate):
                    return 50

    return 0


_STEAM_ICON_CACHE = {}
_STEAM_NEGATIVE_CACHE = {}
_STEAM_MANIFEST_CACHE = {"ts": 0.0, "apps": {}}

_STEAM_NON_GAME_APPIDS = frozenset({
    "228980",   # Steamworks Common Redistributables
    "1070560",  # Steam Linux Runtime 1.0 (scout)
    "1391110",  # Steam Linux Runtime 2.0 (soldier)
    "1628350",  # Steam Linux Runtime 3.0 (sniper)
    "4183110",  # Steam Linux Runtime 4.0
    "1493710",  # Proton Experimental
    "2348590",  # Proton Hotfix
    "250820",   # SteamVR
})


def is_steam_runtime_or_tool(appid: str, name: str) -> bool:
    """Returns True if a Steam manifest entry is a Proton/Runtime/Redistributable tool rather than a game."""
    aid = str(appid or "").strip()
    if aid in _STEAM_NON_GAME_APPIDS:
        return True
    nm = str(name or "").strip().lower()
    if not nm:
        return True
    non_game_prefixes = (
        "proton ",
        "proton-",
        "steam linux runtime",
        "steamworks common redistributables",
        "steamvr",
        "steam controller",
    )
    return nm.startswith(non_game_prefixes)


def _is_steam_scan_enabled() -> bool:
    try:
        from wavecontroller.engine.config_manager import config_manager
        if not config_manager.get("enable_game_channels", True):
            return False
        return bool(config_manager.get("scan_steam_games", True))
    except Exception:
        return True


def _get_steam_manifests_map(force: bool = False) -> dict:
    """Returns a cached mapping of Steam AppID -> {'name': str, 'installdir': str} across all local Steam libraries."""
    if not _is_steam_scan_enabled() and not force:
        return {}

    now = time.monotonic()
    if not force and _STEAM_MANIFEST_CACHE["apps"] and (now - _STEAM_MANIFEST_CACHE["ts"]) < 30.0:
        return _STEAM_MANIFEST_CACHE["apps"]

    steam_roots = [
        os.path.expanduser("~/.local/share/Steam"),
        os.path.expanduser("~/.steam/steam"),
        os.path.expanduser("~/.var/app/com.valvesoftware.Steam/.local/share/Steam"),
    ]
    libs = set()
    for sr in steam_roots:
        if os.path.isdir(sr):
            libs.add(sr)
            vdf = os.path.join(sr, "steamapps", "libraryfolders.vdf")
            if os.path.exists(vdf):
                try:
                    with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
                        for p in re.findall(r'"path"\s+"([^"]+)"', f.read()):
                            if os.path.isdir(p):
                                libs.add(p)
                except Exception:
                    pass

    apps = {}
    for lib in libs:
        steamapps_dir = os.path.join(lib, "steamapps")
        if not os.path.isdir(steamapps_dir):
            continue
        try:
            for fname in os.listdir(steamapps_dir):
                if fname.startswith("appmanifest_") and fname.endswith(".acf"):
                    fpath = os.path.join(steamapps_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            txt = f.read()
                        m_id = re.search(r'"appid"\s+"(\d+)"', txt)
                        m_name = re.search(r'"name"\s+"([^"]+)"', txt)
                        m_dir = re.search(r'"installdir"\s+"([^"]+)"', txt)
                        if m_id and m_name:
                            aid = m_id.group(1)
                            gname = m_name.group(1).strip()
                            if not is_steam_runtime_or_tool(aid, gname):
                                apps[aid] = {
                                    "name": gname,
                                    "installdir": m_dir.group(1).strip() if m_dir else "",
                                }
                    except Exception:
                        pass
        except Exception:
            pass

    _STEAM_MANIFEST_CACHE["ts"] = now
    _STEAM_MANIFEST_CACHE["apps"] = apps
    return apps


def resolve_steam_app_id(app_name: str = None, steam_app_id: str = None) -> str:
    """Resolves a Steam AppID from either an explicit ID or a game/installdir name."""
    if steam_app_id and str(steam_app_id).strip().isdigit():
        return str(steam_app_id).strip()
    if not app_name:
        return ""

    manifests = _get_steam_manifests_map()
    q_raw = str(app_name).strip().lower()
    q_clean = re.sub(r"[^a-z0-9]+", "", q_raw)
    if not q_clean:
        return ""

    for aid, info in manifests.items():
        nm_low = info["name"].lower()
        dir_low = info["installdir"].lower()
        nm_clean = re.sub(r"[^a-z0-9]+", "", nm_low)
        dir_clean = re.sub(r"[^a-z0-9]+", "", dir_low)
        if q_raw == nm_low or q_raw == dir_low or (len(q_clean) >= 4 and (q_clean == nm_clean or q_clean == dir_clean)):
            return aid
    return ""


def is_game_app_or_channel(
    name: str = "",
    binary: str = "",
    exe_name: str = "",
    steam_app_id: str = "",
    steam_game_name: str = "",
    media_role: str = "",
    category: str = "",
    icon: str = "",
) -> bool:
    """
    Smart game classifier: returns True when an application stream or channel
    represents a game (via category, Steam metadata, Proton/Wine .exe, media.role, or Steam manifests).
    """
    if str(category or "").strip().lower() == "game":
        return True
    if str(icon or "").startswith("steam_icon_") or icon == "input-gaming-symbolic":
        return True

    nm_low = str(name or "").strip().lower()
    bin_low = str(binary or "").strip().lower().split("/")[-1].split("\\")[-1]

    # Never classify the Steam client itself or known desktop/chat/browser apps as a game
    all_known_apps = get_known_audio_applications()
    if nm_low in ("steam", "steamwebhelper") or bin_low in DEDICATED_NON_GAME_BINARIES or bin_low in all_known_apps:
        return False
    for k, (disp, _) in all_known_apps.items():
        if disp.lower() == nm_low and (k in DEDICATED_NON_GAME_BINARIES or k in all_known_apps):
            return False

    if steam_app_id or steam_game_name:
        return True
    if str(media_role or "").strip().lower() == "game":
        return True
    if exe_name and str(exe_name).lower() not in _WINE_SYSTEM_EXES:
        return True
    if bin_low.endswith(".exe") and bin_low not in _WINE_SYSTEM_EXES:
        return True
    if resolve_steam_app_id(name):
        return True
    if binary and resolve_steam_app_id(bin_low):
        return True
    return False


def resolve_steam_game_icon(app_name: str = None, steam_app_id: str = None, batch_mode: bool = False) -> str:
    """
    Resolves and extracts the official Steam game icon for a given game name or Steam AppID,
    registering it as 'steam_icon_<appid>' in ~/.config/WaveController/icons and hicolor.
    Returns the icon name ('steam_icon_<appid>') if found, or '' otherwise.
    """
    cache_key = (str(app_name or "").strip().lower(), str(steam_app_id or "").strip())
    if cache_key in _STEAM_ICON_CACHE and _STEAM_ICON_CACHE[cache_key]:
        return _STEAM_ICON_CACHE[cache_key]

    now = time.monotonic()
    neg_ts = _STEAM_NEGATIVE_CACHE.get(cache_key, 0.0)
    if neg_ts and (now - neg_ts) < 15.0:
        return ""

    matched_appid = resolve_steam_app_id(app_name, steam_app_id)
    if not matched_appid:
        if len(_STEAM_NEGATIVE_CACHE) > 512:
            _STEAM_NEGATIVE_CACHE.clear()
        _STEAM_NEGATIVE_CACHE[cache_key] = now
        return ""

    icon_name = f"steam_icon_{matched_appid}"
    wc_icon_dir = os.path.expanduser("~/.config/WaveController/icons")
    hicolor_dir = os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps")
    dest_wc = os.path.join(wc_icon_dir, f"{icon_name}.png")
    dest_hc = os.path.join(hicolor_dir, f"{icon_name}.png")

    newly_created = False
    if not os.path.exists(dest_wc):
        if not _is_steam_scan_enabled():
            return ""

        steam_roots = [
            os.path.expanduser("~/.local/share/Steam"),
            os.path.expanduser("~/.steam/steam"),
            os.path.expanduser("~/.var/app/com.valvesoftware.Steam/.local/share/Steam"),
        ]
        src_ico = None
        src_jpg = None
        src_sub_jpg = None
        for sr in steam_roots:
            cache_dir = os.path.join(sr, "appcache", "librarycache", matched_appid)
            if os.path.isdir(cache_dir):
                try:
                    for fname in os.listdir(cache_dir):
                        full_p = os.path.join(cache_dir, fname)
                        if os.path.isfile(full_p) and fname.lower().endswith(".jpg") and not fname.lower().startswith(("library_", "header")):
                            base = os.path.splitext(fname)[0]
                            if src_jpg is None:
                                src_jpg = full_p
                            if len(base) == 40:
                                cand_ico = os.path.join(sr, "steam", "games", f"{base}.ico")
                                if os.path.exists(cand_ico):
                                    src_ico = cand_ico
                                    break
                        elif os.path.isdir(full_p) and src_sub_jpg is None:
                            for sub_cand in ("library_capsule.jpg", "library_header.jpg", "header.jpg"):
                                sp = os.path.join(full_p, sub_cand)
                                if os.path.isfile(sp):
                                    src_sub_jpg = sp
                                    break
                except Exception:
                    pass
            if src_ico:
                break

        src_file = src_ico or src_jpg or src_sub_jpg
        if src_file and os.path.exists(src_file):
            try:
                os.makedirs(wc_icon_dir, exist_ok=True)
                os.makedirs(hicolor_dir, exist_ok=True)
                from PIL import Image
                im = Image.open(src_file)
                if src_file.lower().endswith(".ico") and hasattr(im, "info") and "sizes" in im.info:
                    sizes = sorted(im.info["sizes"], key=lambda s: s[0] * s[1], reverse=True)
                    if sizes:
                        im.size = sizes[0]
                        im.load()
                im = im.convert("RGBA")
                # Center-crop non-square capsule/header fallbacks and cap icon size to 64x64 to save RAM
                w, h = im.size
                if w != h and w > 0 and h > 0:
                    side = min(w, h)
                    left = (w - side) // 2
                    top = (h - side) // 2
                    im = im.crop((left, top, left + side, top + side))
                if im.size[0] > 64 or im.size[1] > 64:
                    resample_filter = getattr(Image, "Resampling", Image).LANCZOS
                    im = im.resize((64, 64), resample_filter)
                im.save(dest_wc, "PNG")
                newly_created = True
                try:
                    im.save(dest_hc, "PNG")
                except Exception:
                    pass
            except Exception:
                pass

    if os.path.exists(dest_wc):
        if newly_created and not batch_mode:
            try:
                from wavecontroller.utils.game_artwork import refresh_gtk_icon_theme
                refresh_gtk_icon_theme()
            except Exception:
                pass
        if len(_STEAM_ICON_CACHE) > 512:
            _STEAM_ICON_CACHE.clear()
        _STEAM_ICON_CACHE[cache_key] = icon_name
        _STEAM_NEGATIVE_CACHE.pop(cache_key, None)
        return icon_name

    if len(_STEAM_NEGATIVE_CACHE) > 512:
        _STEAM_NEGATIVE_CACHE.clear()
    _STEAM_NEGATIVE_CACHE[cache_key] = now
    return ""


def sync_installed_steam_icons_async(on_complete=None):
    """
    Pre-warms Steam game icons for all installed Steam games in a low-priority background thread
    and refreshes Gtk.IconTheme once at the end so game icons appear instantaneously.
    """
    if not _is_steam_scan_enabled():
        if callable(on_complete):
            from gi.repository import GLib
            GLib.idle_add(on_complete)
        return

    def _worker():
        try:
            manifests = _get_steam_manifests_map(force=True)
            for aid, info in manifests.items():
                resolve_steam_game_icon(info.get("name", ""), aid, batch_mode=True)
            from wavecontroller.utils.game_artwork import refresh_gtk_icon_theme
            refresh_gtk_icon_theme()
        except Exception:
            pass
        if callable(on_complete):
            try:
                from gi.repository import GLib
                GLib.idle_add(on_complete)
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()



