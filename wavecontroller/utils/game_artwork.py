"""
Game Artwork, Banner Compositor & Contrast Engine
=================================================
Handles safe GTK4 game icon rendering, local Steam librarycache banner discovery,
optional SteamGridDB hero/grid fetching, horizontal alpha-fade banner composition,
and automatic WCAG luminance contrast detection for ChannelCard text and symbolic icons.
"""

import os
import re
import json
import hashlib
import threading
import urllib.request
import urllib.parse
from typing import Optional, Tuple, Callable

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib

from wavecontroller.utils.logger import get_logger

log = get_logger("GameArtwork")

WC_ICONS_DIR = os.path.expanduser("~/.config/WaveController/icons")
WC_BANNERS_DIR = os.path.expanduser("~/.config/WaveController/banners")
HICOLOR_256_DIR = os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps")

_BANNER_META_CACHE = {}
_SGDB_INFLIGHT = set()
_SGDB_LOCK = threading.Lock()


def refresh_gtk_icon_theme():
    """
    Forces GTK4's Gtk.IconTheme to rescan search paths immediately so newly extracted
    game icons never render as 'image-missing' (the slashed circle).
    Safe to call from any thread.
    """
    def _do_refresh():
        try:
            display = Gdk.Display.get_default()
            if not display:
                return False
            theme = Gtk.IconTheme.get_for_display(display)
            search_paths = list(theme.get_search_path() or [])
            for d in (WC_ICONS_DIR, HICOLOR_256_DIR):
                if os.path.isdir(d) and d not in search_paths:
                    search_paths.append(d)
            theme.set_search_path(search_paths)
        except Exception as e:
            log.debug(f"Gtk.IconTheme refresh skipped: {e}")
        return False

    if threading.current_thread() is threading.main_thread():
        _do_refresh()
    else:
        GLib.idle_add(_do_refresh)


def _find_icon_file_on_disk(icon_name: str) -> Optional[str]:
    """Locates a PNG/SVG icon file directly on disk if Gtk.IconTheme hasn't indexed it yet."""
    if not icon_name:
        return None
    if os.path.isabs(icon_name) and os.path.isfile(icon_name):
        return icon_name

    candidates = [
        os.path.join(WC_ICONS_DIR, f"{icon_name}.png"),
        os.path.join(WC_ICONS_DIR, f"{icon_name}.svg"),
        os.path.join(HICOLOR_256_DIR, f"{icon_name}.png"),
    ]
    for size_dir in ("256x256", "128x128", "64x64", "48x48", "32x32"):
        candidates.append(
            os.path.expanduser(f"~/.local/share/icons/hicolor/{size_dir}/apps/{icon_name}.png")
        )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def set_gtk_image_safe(
    img_widget: Gtk.Image,
    icon_name: str,
    pixel_size: int = 20,
    fallback_icon: str = "input-gaming-symbolic"
):
    """
    Populates a Gtk.Image from an icon name or direct PNG file fallback so that
    GTK's 'image-missing' slashed circle never appears.
    """
    img_widget.set_pixel_size(pixel_size)
    if not icon_name:
        img_widget.set_from_icon_name(fallback_icon)
        return

    try:
        display = Gdk.Display.get_default()
        if display:
            theme = Gtk.IconTheme.get_for_display(display)
            if theme.has_icon(icon_name):
                img_widget.set_from_icon_name(icon_name)
                return

        disk_file = _find_icon_file_on_disk(icon_name)
        if disk_file:
            refresh_gtk_icon_theme()
            if display and Gtk.IconTheme.get_for_display(display).has_icon(icon_name):
                img_widget.set_from_icon_name(icon_name)
                return
            texture = Gdk.Texture.new_from_filename(disk_file)
            img_widget.set_from_paintable(texture)
            return
    except Exception:
        pass

    # If it looks like an unindexed steam_icon_*, fall back cleanly to gaming controller symbolic
    if str(icon_name).startswith("steam_icon_"):
        img_widget.set_from_icon_name(fallback_icon)
    else:
        img_widget.set_from_icon_name(icon_name or fallback_icon)


def find_local_steam_banner_source(steam_app_id: str) -> Optional[str]:
    """
    Finds the best wide banner source image ('library_hero.jpg', 'library_header.jpg',
    'header.jpg', or 'library_capsule.jpg') in Steam's local appcache/librarycache/<appid>/.
    """
    if not steam_app_id or not str(steam_app_id).strip().isdigit():
        return None
    appid = str(steam_app_id).strip()

    steam_roots = [
        os.path.expanduser("~/.local/share/Steam"),
        os.path.expanduser("~/.steam/steam"),
        os.path.expanduser("~/.var/app/com.valvesoftware.Steam/.local/share/Steam"),
    ]

    preferred_names = (
        "library_hero.jpg",
        "library_hero.png",
        "library_header.jpg",
        "library_header.png",
        "header.jpg",
        "header.png",
        "library_capsule.jpg",
    )

    for sr in steam_roots:
        cache_dir = os.path.join(sr, "appcache", "librarycache", appid)
        if not os.path.isdir(cache_dir):
            continue

        found_by_name = {}
        try:
            for entry in os.listdir(cache_dir):
                full_entry = os.path.join(cache_dir, entry)
                if os.path.isfile(full_entry):
                    low = entry.lower()
                    if low in preferred_names and low not in found_by_name:
                        found_by_name[low] = full_entry
                elif os.path.isdir(full_entry):
                    try:
                        for sub in os.listdir(full_entry):
                            sub_low = sub.lower()
                            if sub_low in preferred_names and sub_low not in found_by_name:
                                found_by_name[sub_low] = os.path.join(full_entry, sub)
                    except Exception:
                        pass
        except Exception:
            pass

        for pname in preferred_names:
            if pname in found_by_name:
                return found_by_name[pname]

    return None


def compose_and_analyze_banner(
    source_image_path: str,
    cache_id: str,
    width: int = 510,
    height: int = 54,
    force_rebuild: bool = False,
    is_custom: bool = False
) -> Tuple[Optional[str], str]:
    """
    Crops and scales `source_image_path` into a `width x height` RGBA banner PNG with a smooth
    horizontal alpha fade on the right half so card controls remain on the dark card background.
    Also analyzes the left text region luminance to return ('light_fg' or 'dark_fg').

    For custom artwork (`is_custom=True`), anchors 1:1 to left edge (left=0) with zero left crop,
    preserves 100% native brightness (zero scrim), and extends full vibrancy past the center.

    Returns: (composed_png_path, contrast_mode)
      where contrast_mode is 'dark_fg' (dark text/icons for bright backgrounds)
      or 'light_fg' (white text/icons for dark backgrounds).
    """
    if not source_image_path or not os.path.isfile(source_image_path):
        return None, "light_fg"

    try:
        src_mtime = int(os.path.getmtime(source_image_path))
    except OSError:
        src_mtime = 0

    custom_flag_str = "custom" if is_custom else "std"
    sig = hashlib.sha1(f"{source_image_path}:{src_mtime}:{width}x{height}:{custom_flag_str}:v4".encode("utf-8")).hexdigest()[:12]
    safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(cache_id))
    os.makedirs(WC_BANNERS_DIR, exist_ok=True)
    out_png = os.path.join(WC_BANNERS_DIR, f"banner_{safe_id}_{sig}.png")
    meta_key = out_png

    if not force_rebuild and os.path.isfile(out_png) and meta_key in _BANNER_META_CACHE:
        return out_png, _BANNER_META_CACHE[meta_key]

    try:
        from PIL import Image
        im = Image.open(source_image_path).convert("RGBA")
        src_w, src_h = im.size
        if src_w <= 0 or src_h <= 0:
            return None, "light_fg"

        resample_filter = getattr(Image, "Resampling", Image).LANCZOS

        if is_custom:
            # 1:1 Custom Artwork: Anchor strictly to left edge (left=0) with zero left crop
            scale = height / float(src_h)
            scaled_w = int(round(src_w * scale))
            art_resized = im.resize((scaled_w, height), resample_filter)

            if scaled_w >= width:
                card_art = art_resized.crop((0, 0, width, height))
            else:
                card_art = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                card_art.paste(art_resized, (0, 0))

            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            canvas.paste(card_art, (0, 0))

            # Alpha fade strictly behind right controls (x >= 320 to 480)
            fade_start = 320
            fade_end = 480
            alpha_band = Image.new("L", (width, 1), 255)
            for x in range(width):
                if x <= fade_start:
                    a_val = 255
                elif x >= fade_end:
                    a_val = 40
                else:
                    t = (x - fade_start) / float(fade_end - fade_start)
                    smooth = 1.0 - (t * t * (3.0 - 2.0 * t))
                    a_val = int(round(40 + (255 - 40) * smooth))
                alpha_band.putpixel((x, 0), a_val)

            alpha_mask = alpha_band.resize((width, height))
            canvas.putalpha(alpha_mask)

            # Custom art: Zero dark scrim overlay. 100% native brightness and contrast!
            contrast_mode = "light_fg"
        else:
            # Visible art spans roughly the left 360px (fading to 0 by x=360)
            art_w = min(width, 365)
            target_ratio = art_w / float(height)
            src_ratio = src_w / float(src_h)

            if src_ratio > target_ratio:
                # Source is wider than target strip: crop horizontally around center
                new_w = int(round(src_h * target_ratio))
                left = max(0, (src_w - new_w) // 2)
                im_cropped = im.crop((left, 0, left + new_w, src_h))
            else:
                # Source is taller than target strip: crop vertically slightly above center (focal zone ~38%)
                new_h = max(1, int(round(src_w / target_ratio)))
                top = max(0, min(src_h - new_h, int((src_h - new_h) * 0.38)))
                im_cropped = im.crop((0, top, src_w, top + new_h))

            art_resized = im_cropped.resize((art_w, height), resample_filter)

            # Analyze left text/icon region (x: 8..225, y: 6..height-6) for WCAG relative luminance
            sample_box = art_resized.crop((8, 6, min(225, art_w), max(7, height - 6))).resize((48, 12), resample_filter)
            pixels = list(sample_box.getdata())
            lums = []
            for r, g, b, a in pixels:
                # Relative perceptual luminance (0.0 = black, 1.0 = white)
                lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
                lums.append(lum)

            mean_lum = sum(lums) / len(lums) if lums else 0.2
            contrast_mode = "dark_fg" if mean_lum >= 0.50 else "light_fg"

            # Build full 510x54 RGBA canvas with smooth horizontal fade from x=165..360
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            canvas.paste(art_resized, (0, 0))

            fade_start = 165
            fade_end = art_w
            alpha_band = Image.new("L", (width, 1), 0)
            for x in range(width):
                if x <= fade_start:
                    a_val = 235  # Slight blend with card surface
                elif x >= fade_end:
                    a_val = 0
                else:
                    t = (x - fade_start) / float(fade_end - fade_start)
                    # Smoothstep easing (1 -> 0)
                    smooth = 1.0 - (t * t * (3.0 - 2.0 * t))
                    a_val = int(round(235 * smooth))
                alpha_band.putpixel((x, 0), a_val)

            alpha_mask = alpha_band.resize((width, height))
            r_ch, g_ch, b_ch, existing_a = canvas.split()
            # Combine existing alpha with horizontal fade mask
            final_a = Image.eval(alpha_mask, lambda v: v)
            canvas.putalpha(final_a)

            # Fast 1D horizontal scrim band over the left text area (x: 0..220) to guarantee white text contrast
            scrim_band = Image.new("RGBA", (width, 1), (0, 0, 0, 0))
            for x in range(width):
                if x <= 140:
                    s_val = 140  # ~55% dark overlay
                elif x <= 220:
                    t = (x - 140) / 80.0
                    s_val = int(round(140 * (1.0 - t)))
                else:
                    s_val = 0
                scrim_band.putpixel((x, 0), (15, 15, 20, s_val))
            scrim = scrim_band.resize((width, height))
            canvas = Image.alpha_composite(canvas, scrim)

            contrast_mode = "light_fg"

        canvas.save(out_png, "PNG")
        _BANNER_META_CACHE[meta_key] = contrast_mode
        return out_png, contrast_mode
    except Exception as e:
        log.warning(f"Failed to compose banner from '{source_image_path}': {e}")
        return None, "light_fg"


def get_steamgriddb_raw_path(steam_app_id: str = "", game_name: str = "") -> str:
    """Returns the expected path of the raw SteamGridDB downloaded image."""
    cache_token = str(steam_app_id or game_name).strip().lower()
    safe_tok = re.sub(r"[^a-z0-9_-]+", "_", cache_token)
    return os.path.join(WC_BANNERS_DIR, f"sgdb_raw_{safe_tok}.png")


def has_steamgriddb_banner(steam_app_id: str = "", game_name: str = "") -> bool:
    """Checks if a downloaded SteamGridDB raw image exists locally."""
    raw_p = get_steamgriddb_raw_path(steam_app_id, game_name)
    return os.path.isfile(raw_p) and os.path.getsize(raw_p) > 512


def purge_channel_artwork(channel_id: str, steam_app_id: str = "", game_name: str = ""):
    """
    Deletes downloaded SteamGridDB raw images and all composed banner images
    associated with the given channel and app ID from ~/.config/WaveController/banners/.
    """
    try:
        tokens = set()
        if steam_app_id:
            tokens.add(str(steam_app_id).strip().lower())
        if game_name:
            tokens.add(str(game_name).strip().lower())

        for tok in tokens:
            safe_tok = re.sub(r"[^a-z0-9_-]+", "_", tok)
            raw_path = os.path.join(WC_BANNERS_DIR, f"sgdb_raw_{safe_tok}.png")
            tmp_path = f"{raw_path}.tmp"
            for p in (raw_path, tmp_path):
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                        log.debug(f"Purged raw SGDB banner: {p}")
                    except OSError:
                        pass

        safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(channel_id))
        prefix = f"banner_{safe_id}_"
        if os.path.isdir(WC_BANNERS_DIR):
            for fname in os.listdir(WC_BANNERS_DIR):
                if fname.startswith(prefix) and fname.endswith(".png"):
                    full_p = os.path.join(WC_BANNERS_DIR, fname)
                    try:
                        os.remove(full_p)
                        log.debug(f"Purged composed banner: {full_p}")
                    except OSError:
                        pass
                    _BANNER_META_CACHE.pop(full_p, None)
    except Exception as e:
        log.debug(f"Error purging channel artwork for '{channel_id}': {e}")


def fetch_steamgriddb_banner_async(
    api_key: str,
    game_name: str,
    steam_app_id: str = "",
    on_complete: Optional[Callable[[Optional[str]], None]] = None
):
    """
    Asynchronously downloads a wide Hero or Grid banner from SteamGridDB v2 API
    into ~/.config/WaveController/banners/ and invokes `on_complete(local_raw_path)` on the GTK main loop.
    """
    clean_key = (api_key or "").strip()
    if not clean_key:
        if on_complete:
            GLib.idle_add(on_complete, None)
        return

    cache_token = str(steam_app_id or game_name).strip().lower()
    safe_tok = re.sub(r"[^a-z0-9_-]+", "_", cache_token)
    os.makedirs(WC_BANNERS_DIR, exist_ok=True)
    dest_raw = os.path.join(WC_BANNERS_DIR, f"sgdb_raw_{safe_tok}.png")

    if os.path.isfile(dest_raw) and os.path.getsize(dest_raw) > 512:
        if on_complete:
            GLib.idle_add(on_complete, dest_raw)
        return

    with _SGDB_LOCK:
        if safe_tok in _SGDB_INFLIGHT:
            return
        _SGDB_INFLIGHT.add(safe_tok)

    def _worker():
        result_path = None
        headers = {
            "Authorization": f"Bearer {clean_key}",
            "User-Agent": "WaveController/1.0"
        }

        def _api_get(url: str) -> Optional[dict]:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    return json.loads(resp.read().decode("utf-8", errors="ignore"))
            except Exception:
                return None

        try:
            image_url = None
            # 1. Try lookup by Steam AppID first (Heroes -> Wide Grids)
            if steam_app_id and str(steam_app_id).isdigit():
                for endpoint in (
                    f"https://www.steamgriddb.com/api/v2/heroes/steam/{steam_app_id}",
                    f"https://www.steamgriddb.com/api/v2/grids/steam/{steam_app_id}?dimensions=460x215,920x430",
                ):
                    data = _api_get(endpoint)
                    if data and data.get("success") and data.get("data"):
                        image_url = data["data"][0].get("url")
                        if image_url:
                            break

            # 2. Fallback: autocomplete search by game name
            if not image_url and game_name:
                enc_name = urllib.parse.quote(str(game_name).strip())
                search_data = _api_get(f"https://www.steamgriddb.com/api/v2/search/autocomplete/{enc_name}")
                if search_data and search_data.get("success") and search_data.get("data"):
                    sgdb_game_id = search_data["data"][0].get("id")
                    if sgdb_game_id:
                        for endpoint in (
                            f"https://www.steamgriddb.com/api/v2/heroes/game/{sgdb_game_id}",
                            f"https://www.steamgriddb.com/api/v2/grids/game/{sgdb_game_id}?dimensions=460x215,920x430",
                        ):
                            data = _api_get(endpoint)
                            if data and data.get("success") and data.get("data"):
                                image_url = data["data"][0].get("url")
                                if image_url:
                                    break

            if image_url:
                req = urllib.request.Request(image_url, headers={"User-Agent": "WaveController/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw_bytes = resp.read()
                if raw_bytes and len(raw_bytes) > 512:
                    tmp_path = f"{dest_raw}.tmp"
                    with open(tmp_path, "wb") as f:
                        f.write(raw_bytes)
                    os.replace(tmp_path, dest_raw)
                    result_path = dest_raw
        except Exception as e:
            log.debug(f"SteamGridDB banner fetch failed for '{game_name}': {e}")
        finally:
            with _SGDB_LOCK:
                _SGDB_INFLIGHT.discard(safe_tok)

        if on_complete:
            GLib.idle_add(on_complete, result_path)

    threading.Thread(target=_worker, daemon=True).start()
