# Changelog

All notable changes to WaveController are documented in this file.

## [0.0.3.4] - 2026-09-12

### Fixed
- **Application picker no longer shows stale browser / renderer noise.** The app list now filters to running audio-capable applications and excludes background Chrome renderer/GPU helpers, which previously made Chrome appear even when it was not producing audio.
- **Signal no longer appears as an available app.** The background non-audio utility filter removes Signal and similar non-audio helpers from the app picker while keeping real audio apps visible.
- **Idle audio-capable apps remain discoverable.** Spotify and similar apps can now be listed while they are running but not currently playing, without reintroducing the stale browser false positives.

### Changed
- Simplified the PipeWire app discovery hot path to reduce unnecessary process scanning and tighten the app filter for better responsiveness and snappier UI behavior.
- Trimmed dead variables and unnecessary work in the app list refresh path to keep the channel creation UI feeling lighter during app discovery.

### Internal / Maintenance
- Reduced redundant work in the app discovery and refresh pipeline.
- Removed leftover logic from the previous broad app-listing approach that had drifted away from the intended audio-only behavior.

## [0.0.3.3] - 2026-09-12

### Fixed
- **Chrome/new-tab routing now reacts in real time.** Added a `pw-mon`-backed stream watcher that immediately reconciles new application audio streams, removing the audible delay that could happen when Chrome or YouTube created a fresh playback stream before the periodic PipeWire poll noticed it.

### Internal / Maintenance
- Reused the fast reconcile path's existing PipeWire port/link snapshots when syncing channel audio routing, avoiding redundant subprocess round-trips during new-stream handling.
- Moved plugin bundle install validation off the GTK main thread so large VST3/LV2 folder installs do not stutter the Effects Manager UI.
- Removed unused imports from the plugin FX chain and Effects Manager modules.

## [0.0.3.2] - 2026-09-10

### Added
- **Per-effect intensity dial**: each DSP effect in the channel FX popover now has its own rotary dial (drag vertically, scroll wheel to nudge, double-click to reset) controlling how aggressively that effect is applied, in addition to its existing on/off toggle. Includes a 50% neutral-position marker and dims visually when its effect is disabled.
- **Plugin management ("Install Effect")**: Effects Manager can now install external VST3/LV2 plugin bundles via a folder picker or drag-and-drop. Installed plugins are symlinked into `~/.vst3`/`~/.lv2` and tracked in a manifest so they can be safely removed later — built-in effects, system-found plugins, and pre-existing user-directory plugins are never removable, only ones the app itself installed.
- **Custom plugin scan paths**: Effects Manager can now scan additional user-specified directories for VST3/LV2 bundles (e.g. Flatpak-sandboxed or DAW-bundled plugin folders).

### Changed
- **OOBE Page 5 redesigned**: replaced the redundant "Primary Input/Output Device" dropdowns with the same "Allow WaveController to set system input/output defaults?" toggle used in Settings. Primary device selection is now derived automatically from the hardware chosen on Page 4 instead of asking twice.
- **"Indexed Plugins Library" now hides internal duplicates**: Built-in effects (already shown as toggles in the DSP Chain section) and the raw LADSPA re-discovery of WaveController's own bundled engine files no longer clutter the plugin list — only genuinely external/user plugins are shown.

### Fixed
- **FX master enable/disable toggle didn't reliably stop the DSP chain.** `reload_channel_fx()` treated the literal channel id `"mic"` as a "no specific channel" sentinel and always re-derived the channel list via heuristics instead of trusting a real, currently-registered channel id — made the toggle's effective behavior fragile and hard to diagnose. Now resolves a known channel id directly.
- **Effects Manager's global per-effect toggles didn't actually disable effects for channels with a stale per-channel override.** An effect turned off globally could still run if a channel's saved FX record had that effect explicitly enabled. The global toggle is now a hard kill-switch (`enabled = global AND per-channel`), and per-channel FX popovers hide rows for globally-disabled effects, refreshing live the instant a global toggle changes (no restart or channel recreation needed).
- **Mic silence / audio routing drift after extended sessions**, root-caused to config state (mix/channel ids, per-channel FX flags) drifting out of sync with the live PipeWire graph over long-running sessions with lots of manual reconfiguration. Documented the recurring symptom and the reliable fix (full config wipe + reinstall) in repo memory for faster recovery; a lighter in-app "Reset Audio Routing" action was identified as a good follow-up but not built this session.

### Internal / Maintenance
- Removed 3 confirmed-dead functions with zero call sites anywhere in the repo: `reconcile_meter_ports()` (`stream_resolver.py`), `bind_app_to_target_sink()`/`unbind_app_from_target_sink()` (`app_tracker.py`), `sync_source_to_mixes()` (`source_manager.py`) — all abandoned duplicate/experimental routing paths superseded by the logic actually running in `pipewire_manager.py`.
- Removed unused legacy attributes `output_devices`/`connected_audio_devices` from `usb_hardware.py` (written but never read anywhere).
- Added timeouts to the hot-path PipeWire routing sync subprocess calls (`pw-link`, `pw-dump`, `wpctl`) so a hung call can no longer freeze the background sync thread indefinitely.
- Hoisted a redundant per-channel×mix `pw-dump` call in `_sync_channel_audio_routing()`'s link-verification step to run once per sync pass instead of once per channel/mix combination.
- Fixed two test-isolation bugs in `tests/test_fx_chain.py` that depended on the real, mutable `~/.config/WaveController/config.json` instead of mocking `config_manager` — these were exposed (not caused) by the FX kill-switch fix above.
- Updated `WaveController_PipeWire_Node_Reference.txt`: added the pre-fader FX chain node/routing section, live-verified the Group Channel contract against a real capture, documented the Discord-reports-as-"Chromium" PipeWire naming quirk, and recorded both bug fixes above.
- Full test suite passes (86/86) after every change this session.

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
