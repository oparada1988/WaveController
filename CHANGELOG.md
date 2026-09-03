# Changelog

All notable changes to WaveController are documented in this file.

## [0.0.3.1] - 2026-09-03

### Fixed
- **"Chat Mix" (and other input mixes) no longer appear as an Output device** in GNOME Settings and OBS. The virtual `_Source` node was created with `media.class=Audio/Duplex`, which exposes both playback and capture ports and is therefore listed under both Output and Input device pickers. It now uses `media.class=Audio/Source/Virtual`, which only ever appears under Sources/Inputs. Routing (submix loopback -> mix node) required no logic changes since the link-matching code already handled both port-naming contracts.
- **GNOME Input default silently failed to persist across reboots for input mixes.** `set_mix_system_default()` built the persisted PipeWire metadata key as `default.configured.default.audio.source` (a leftover double `"default."` prefix bug) instead of the correct `default.configured.audio.source`, so the actual key GNOME/WirePlumber reads for the saved Input default was never updated. Output/sink mixes were unaffected because `wpctl set-default` succeeds for sinks and updates that key itself. Fixed by writing the correct key name directly.

## [0.0.3.0] - 2026-09-03 (Alpha 3)

### Added
- **Sidebar slide animation**: collapsing/expanding the sidebar now smoothly animates its width (220ms, ease-out-cubic via `Adw.TimedAnimation`) instead of snapping instantly between 68px/225px. Labels hide immediately on collapse and reveal once the expand animation completes, avoiding text clipping mid-transition.
- **`wavecontroller/utils/css_helpers.py`**: shared `install_palette_css()` helper for installing solid-color CSS classes from a palette, used by both the LED ring color picker and mix accent color picker.
- **`wavecontroller/utils/gtk_helpers.py`**: shared `blocked_handler()` context manager for temporarily blocking a GTK signal handler while setting a widget's value programmatically (replaces ~13 duplicated `handler_block()`/`try`/`finally`/`handler_unblock()` blocks across `device_settings.py`, `mix_header.py`, and `settings_view.py`).
- **`led_color_picker.build_led_color_row()`**: shared builder for the "label + LED color button" row, used by both the microphone gain LED row (`channel_card.py`) and headphone LED row (`mix_header.py`).

### Changed
- **Background autostart consolidated to systemd user service**: the "Start Automatically on Login" toggle is now "Enable Background Service (systemd)" and drives `systemctl --user enable/disable --now wavecontroller.service` instead of writing an XDG `~/.config/autostart` desktop entry. The systemd unit provides crash auto-restart (`Restart=on-failure`) and waits for PipeWire/WirePlumber to be ready (`After=pipewire.service wireplumber.service`), and requires no `sudo` (user-space unit). Any leftover XDG autostart entry from older versions is automatically cleaned up.
- **Installer consolidation**: removed the stale, divergent `scripts/install.sh` duplicate. The root `install.sh` is now the single canonical installer; `README.md`'s curl bootstrap command has been updated to point at it. `install.sh --autostart` / `--disable-autostart` now manage the systemd unit to match its documented behavior.
- **Window geometry saves are now debounced**: resizing/maximizing the window no longer writes to disk on every single `notify::default-width`/`height` event during a live drag. The final state is still flushed immediately on window close so nothing is lost on quit.

### Fixed
- **Duplicate channel subtitle text**: channels auto-created for a single detected app (e.g. "Google Chrome", "Spotify") no longer show the same name twice (once as the title, once as the subtitle). The subtitle is now hidden when it would just repeat the title, and still shows normally for multi-app group channels.
- **Hardware listener leaks**: `ChannelCard`, `UnifiedDeviceSettingsView`, and `LEDColorButton` now store their `hardware_mgr` listener callback as a named reference and expose `cleanup()`, which is called at every real teardown point (channel deletion in `mixer_matrix.py`, and all 5 device-view teardown paths in `window.py`, now consolidated into one `_teardown_device_views()` helper). Previously, repeatedly removing/re-adding a hardware device would leave stale listener closures registered forever.
- **`NameError` risk in `channel_card.py`**: the hardware listener callback used `GLib.idle_add(...)` but `GLib` was never imported in the file, which would have raised `NameError` at runtime whenever the hardware sync callback fired.
- **Orphaned config `.tmp` files**: `ConfigManager.save_now()` now cleans up the temporary file if the atomic write fails partway through, instead of leaving it behind.
- **Unused dead import**: removed an unused `LEDColorButton` import from `device_settings.py` (it was never instantiated there).

### Internal / Maintenance
- Full audit of the codebase for dead code, duplicate logic, and UI/IO performance issues; most flagged concerns (40 FPS tick loop, grid rebuild cost, CSS provider recreation, subprocess polling frequency, signal-handler-in-rebuild-loop) were verified as already correctly implemented and did not require changes.
- All changes verified via the full audio invariant regression suite (73 tests passed, 2 skipped) and a full-file `get_errors` sweep after each change.

## [0.0.2.5] - 2026-09-02

### Changed
- Improved PipeWire defaults handling and sidebar controls.
- Synchronized mix defaults and header controls to stay consistent after external changes.
- Persisted default input/output device selection across reboots.

### Added
- Added Wave XLR MK2 hardware support and assets.

## [0.0.2.4] - 2026-08-31

### Added
- System default mix controls.
- Zero-bleed metering improvements.

### Fixed
- Port sanitization for mix nodes.
- Restored Audio/Source/Virtual properties for source mix nodes to fix missing input ports.
- Implemented unassigned app fallback routing and submix front-right link verification.

### Changed
- General UI performance optimizations.
