<img width="987" height="347" alt="WaveController-banner" src="https://github.com/user-attachments/assets/fcfb7e80-d887-4def-8a87-501ffbf00e78" />

<h2 align="center"><strong>NOTICE: EARLY STAGES OF DEVELOPMENT</strong></h2>

**WaveController** is a native Linux multi-track audio mixing engine, PipeWire session manager, and hardware controller. It is engineered to deliver the full **Elgato Wave Link** and **Wave Device** tier-1 experience natively on Linux, alongside universal support for third-party USB microphones and audio interfaces.

WaveController is actively developed, human-tested, and audited on production Linux audio workstations. Hardware support currently centers on the **Elgato Wave XLR (MK1)**, with scaffolded support and milestones planned for the Wave XLR MK2, Wave:3, and Wave:1.

<p align="center">
  <a href="https://discord.gg/Mhu29PcPJ">
    <img src="assets/discord-server-banner.png" alt="Join the Discord Server" />
  </a>
</p>
---

### Screenshots

#### Full Mixer Board
<img width="1750" height="1185" alt="WaveController Full Mixer Board" src="https://github.com/user-attachments/assets/ab713660-c190-4dc0-9c23-c9409d873834" />
<img width="1750" height="1185" alt="WaveController Channel Matrix" src="https://github.com/user-attachments/assets/ea92b5b0-ddcb-46d2-90d4-3115ebcae015" />

#### Audio Flow (Interactive Patchbay Graph)
<img width="2050" height="1234" alt="WaveController Audio Flow View" src="https://github.com/user-attachments/assets/926d0511-aadb-4048-a130-430cf833329c" />

#### Compact Mixer Mode

| Vertical Layout | Horizontal Layout |
| :---: | :---: |
| <img width="570" height="473" alt="WaveController Compact Mode Vertical" src="https://github.com/user-attachments/assets/889f6103-2616-4acf-a830-df2bd6c4b707" /> | <img width="480" height="510" alt="WaveController Compact Mode Horizontal" src="https://github.com/user-attachments/assets/e24a5a8a-fe74-4030-8d07-246737e0684d" /> |

---

### Supported Hardware & Compatibility

WaveController organizes hardware support into two distinct operational tiers:

- **Tier 1 (Native Elgato USB Protocol)**: Direct communication with on-device hardware controllers via raw `libusb` control transfers on Endpoint 0 (`wIndex=0x3303`). This bypasses standard Linux kernel ALSA driver locks, permitting real-time control of analog preamp gain, true 48V phantom power, dual-stage hardware Clipguard limiters, low-cut filters, capacitive touch mute events, and customizable RGB LED rings without interrupting audio capture or playback.
- **Tier 2 (Universal USB Microphones & Audio Interfaces)**: Comprehensive compatibility with any standard USB audio device. Missing hardware features (such as analog limiters or high-pass filters) are emulated transparently in software through PipeWire DSP filter graphs.

#### Elgato Wave Device Compatibility Matrix

| Device | USB VID:PID | Support Tier | Preamp Gain | 48V Phantom | Hardware Clipguard | Low-Cut Filter | Capacitive Mute | Zero-Latency Monitoring | Mic/PC Blend | LED Indicator Sync | Status |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **Elgato Wave XLR (MK1)** | `0fd9:007d` | Tier 1 (Native USB) | 0–75 dB Analog | Yes (+48V Rail) | Yes (Dual-Stage Analog) | 80Hz / 120Hz Analog | Yes | Yes (Hardware 3.5mm) | Yes (Hardware Blend) | 360-Degree RGB Ring | **Fully Supported** |
| **Elgato Wave XLR MK2** | `0fd9:00b6` | Tier 1 (Native USB) | 0–75 dB Analog | Yes (+48V Rail) | Yes (Dual-Stage Analog) | 80Hz / 120Hz Analog | Yes | Yes (Hardware 3.5mm) | Yes (Hardware Blend) | 360-Degree RGB Ring | In Development (Beta 1 Target) |
| **Elgato Wave:3** | `0fd9:0070` | Tier 1 (Native USB) | 0–40 dB Analog | N/A (Condenser) | Yes (Dual-Stage Analog) | 80Hz / 120Hz Analog | Yes | Yes (Hardware 3.5mm) | Yes (Hardware Dial) | LED Bar & Dial Ring | In Development (Beta 1 Target) |
| **Elgato Wave:1** | `0fd9:007c` | Tier 1 (Native USB) | Software / ALSA | N/A (Condenser) | Yes (Dual-Stage Analog) | 80Hz / 120Hz Analog | Dial Press | Yes (Hardware 3.5mm) | Software Mix | LED Dial Ring | Planned |
| **Elgato Wave Neo** | `0fd9:00a2` | Tier 1 (Native USB) | Software / ALSA | N/A (Condenser) | Software Limiter | Software High-Pass | Tap-to-Mute | Yes (Hardware 3.5mm) | Software Mix | LED Indicator | Planned |
| **Universal USB Audio Interfaces & Microphones** | Any USB Audio Device | Tier 2 (Universal) | ALSA / Software | Hardware Switch | Emulated Peak Limiter | Emulated High-Pass | Software / Hotkey | Via Hardware / Low-Latency Loopback | Software Mix | UI VU & Status Indicators | **Fully Supported** |

---

### Features

#### 1. Audio Flow View (Interactive Patchbay Graph)
- **Live Node-Graph Canvas**: Visualizes the active PipeWire audio graph, mapping input capture devices, application channel strips, submix matrix buses, and physical output playback endpoints.
- **Dynamic Path Highlighting**: Clicking any audio node highlights its entire upstream and downstream routing tree to visually trace sound flow across the system.
- **Animated Signal Metering**: Active patch lines render real-time PCM audio activity directly on the graph canvas.
- **Automatic Graph Reconciliation**: Tracks PipeWire node lifecycle events (`pw-mon`) to add, update, or remove audio connections dynamically without manual refreshes.

#### 2. Compact Mixer Mode (Horizontal & Vertical Layouts)
- **Floating Mini-Mixer Window**: Minimalist secondary window mode designed for multi-monitor streaming setups and compact screen footprints.
- **Dual Form Factor Orientations**:
  - **Vertical Layout**: Traditional studio fader strips with vertical volume sliders and stacked meters.
  - **Horizontal Layout**: Clean row-based channel strips with horizontal sliders for low-profile placement under a game or stream window.
- **Mix Bus Filtering Dropdown**: Quickly switch between Personal Mix, Stream Mix, Chat Mix, or any configured submix without opening the full mixer board.
- **Channel Telemetry**: Displays live stereo VU meters, channel icons, volume percentages, mute status, and volume link indicators.

#### 3. Multi-Track Virtual Sub-Mixing & Volume Linking
- **Discrete Output Buses**: Manage independent submixes (Personal Mix, Stream Mix, Chat Mix, Recording Mix) with isolated per-channel faders and mute states.
- **Volume Linked Across Mixes ("Link")**: Channels can link fader levels across all submixes. Adjusting a linked channel's volume fader proportionally adjusts its level on all other mixes while strictly preserving existing volume offsets between mixes.
- **Master Bus Output Cards**: Dedicated master output cards for each submix with hardware output sink selection, master volume sliders, master mute controls, and direct monitor routing.

#### 4. Slide-Out FX Inspector Sidebar & Real-Time DSP Rack
- **Animated FX Sidebar**: Smooth 240ms slide-out revealer panel in the mixer matrix with click-away background dismissal.
- **Live DSP Controls**: Real-time intensity sliders (0–100%) and toggle controls for built-in audio processing modules:
  - 10-Band Parametric Equalizer with visual curve response
  - Studio Compressor (threshold, ratio, attack, release, makeup gain)
  - Broadcast Noise Gate (threshold, hysteresis, hold, release)
  - RNNoise Neural Network Noise Suppression (AI-driven background noise and keyboard removal)
  - Studio Vocal De-Esser
  - Fast Lookahead Peak Limiter
  - High-Pass Filter (80Hz / 120Hz selectable cutoff)
- **Rotary Intensity Dials**: Fine-grained rotary dials with neutral center detents, vertical drag, and scroll-wheel nudge support.

#### 5. External Audio Plugin Architecture (VST3 & LV2 Hosting)
- **Plugin Installation Manager**: Install external VST3 and LV2 plugin bundles via drag-and-drop or directory pickers.
- **Safe Symlink Management**: Plugins are linked into `~/.vst3` and `~/.lv2` and tracked in an installation manifest for clean removal.
- **Custom Scan Directories**: Add custom search paths for DAW-bundled, Flatpak, or system-wide audio plugin folders.
- **Deduplicated Plugin Index**: External plugin browser isolates user-installed plugins from internal system duplicates.

#### 6. Unix IPC Socket API & Stream Deck / Volume Controller Plus Integration
- **High-Performance Unix Domain Socket**: Local control socket located at `~/.config/WaveController/wavecontroller.sock` (with symlinks at `$XDG_RUNTIME_DIR/wavecontroller.sock` and `/tmp/wavecontroller.sock`).
- **Stream Deck + Integration**: Native two-way integration with the Volume Controller Plus plugin on StreamController.
- **Rotary Encoder & LCD Telemetry**: Physical rotary encoders adjust volume with dial-push to mute; LCD touchscreen strips display live stereo VU meters, channel names, volume percentages, and dynamic status badges ("Default Input", "Default Output").
- **Wave XLR Touchscreen Controls**: Hardware slices display stacked 48V phantom power buttons and channel volume link toggle buttons with instant touch interaction.
- **Granular IPC Protocol**: JSON command schema supporting `get_fx_status`, `toggle_fx`, `toggle_channel_effect`, `set_channel_effect`, `link_channel_volume`, and `unlink_channel_volume`.

#### 7. Virtual System Output Sinks & Desktop Audio Fallback
- **Direct Sound Card Exposure**: Single application channels can be exposed as independent virtual PipeWire sound cards (`media.class="Audio/Sink"`), enabling discrete routing inside external software (OBS, Discord, games) without requiring a group channel.
- **Desktop Audio Default Sink**: Dedicated virtual sink channel that automatically captures and controls unassigned desktop audio streams.

#### 8. Application Group Channels & Direct Matrix Ingestion
- **Multi-App Bundling**: Combine multiple audio streams (e.g., Discord + Slack + Zoom, or Spotify + Firefox) into a single fader strip with removable stream chips.
- **Clean Audio Menus**: Internal channel ingestion sinks stay contained within WaveController, preventing menu clutter in system settings.

#### 9. Real-Time Stream Routing & Binary Classification
- **Instant Stream Reconcile**: Event-driven `pw-mon` stream monitor immediately routes newly launched applications or browser tabs with zero audible lag.
- **Authoritative Process Classification**: Inspects `application.process.binary` metadata to eliminate collisions between Chromium and Electron applications.

#### 10. Studio-Grade Perceptual VU Metering
- **Broadcast-Calibrated Ballistics**: 40 FPS unbuffered PCM stream metering with calibrated -54 dBFS to 0 dBFS loudness curves.
- **Zero-Bleed Isolation**: Strict channel and submix separation prevents cross-talk across independent monitoring faders.

#### 11. Hardware Disconnect Shield & Power Lifecycle Protection
- **Disconnect Shield**: Prevents physical Wave devices from falsely reporting as offline during dynamic PipeWire profile switches.
- **Resume Protection**: 64-bit ctypes libusb signatures prevent native segmentation faults across system sleep cycles.
- **Resume Mute Shield & Blackout**: Automatically turns off hardware LEDs during sleep and silences hardware outputs on wake until USB registers are fully synchronized, eliminating clicks, pops, and volume spikes.

#### 12. GNOME Panel Recording Indicator Bypass
- Metering taps and internal helper loopbacks report with `application.id=org.PulseAudio.pavucontrol` and `media.role=volume-control`, preventing GNOME Shell from displaying a persistent recording microphone pill on the top panel.

#### 13. Modular Audio Engine & Regression Invariants
- Modular subsystems separate graph resolution, metering capture, and hardware control.
- Automated audio invariant regression test suite verifies routing invariants, zero-bleed contracts, and link stability.
- Pre-commit git hook blocks regressions from entering the codebase.

---

### System Requirements & Dependencies

WaveController requires a modern Linux distribution running PipeWire with WirePlumber session management.

#### Runtime Dependencies
- **Audio Subsystem**: PipeWire (>= 0.3.50), WirePlumber (>= 0.4.10), `pw-link`, `pw-loopback`, `wpctl`
- **Runtime Environment**: Python (>= 3.10)
- **GUI Framework**: GTK 4, Libadwaita 1, PyGObject, Pillow (PIL)
- **System Tools**: `rsync`, `udevadm`

#### Distribution Package Commands

**Ubuntu / Debian / Pop!_OS / Linux Mint**:
```bash
sudo apt update && sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-pil pipewire wireplumber pipewire-bin rsync
```

**Fedora / RHEL**:
```bash
sudo dnf install -y python3-gobject gtk4 libadwaita python3-pillow pipewire wireplumber rsync
```

**Arch Linux / Manjaro**:
```bash
sudo pacman -S --needed python-gobject gtk4 libadwaita python-pillow pipewire wireplumber rsync
```

**openSUSE (Tumbleweed / Leap)**:
```bash
sudo zypper install -y python3-gobject gtk4 typelib-1_0-Adw-1 python3-Pillow pipewire wireplumber rsync
```

---

### Installation & Setup

#### 1. One-Line Remote Installer
Install WaveController, systemd service units, desktop launcher, icons, WirePlumber profiles, and udev hardware permissions:
```bash
curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/install.sh | bash
```

#### 2. Local Repository Installation
Clone the repository and run the installation script:
```bash
git clone https://github.com/oparada1988/WaveController.git
cd WaveController
./install.sh
```

---

### Management Script Options (`install.sh`)

The unified installer manages installation, systemd user services, and upgrades:

| Option | Description |
| :--- | :--- |
| **`./install.sh`** (or `-i`, `--install`) | Checks dependencies, installs application files to `~/.local/share/wavecontroller`, deploys WirePlumber studio profiles, installs the systemd unit, creates CLI launcher `~/.local/bin/wavecontroller`, installs icons, and configures `udev` rules. |
| **`./install.sh -u`** (`--upgrade`) | Pulls latest code from Git, updates application files, refreshes systemd and WirePlumber units, and syncs icons while preserving user configuration. |
| **`./install.sh -r`** (`--uninstall`) | Cleanly stops and removes the systemd service, WirePlumber profile drop-in, desktop menu entry, icons, and application files. |
| **`./install.sh --autostart`** | Enables and starts the user-space systemd unit (`wavecontroller.service`) to run the audio daemon on login. |
| **`./install.sh --disable-autostart`** | Disables and stops the systemd background auto-start service. |
| **`./install.sh -h`** (`--help`) | Displays script usage and path information. |

---

### Standard Installation Paths

- **Application Directory**: `~/.local/share/wavecontroller/`
- **CLI Executable**: `~/.local/bin/wavecontroller`
- **Systemd User Service**: `~/.config/systemd/user/wavecontroller.service`
- **WirePlumber Studio Profile**: `~/.config/wireplumber/wireplumber.conf.d/51-wavecontroller-wave-xlr.conf`
- **Desktop Menu Entry**: `~/.local/share/applications/com.oparada.WaveController.desktop`
- **Desktop Icons**: `~/.local/share/icons/hicolor/` (48x48 through 512x512, scalable, and symbolic)
- **User Configuration**: `~/.config/WaveController/config.json`
- **IPC Socket**: `~/.config/WaveController/wavecontroller.sock`
- **Hardware Udev Permissions**: `/etc/udev/rules.d/99-elgato-wave.rules`
- **Hardware Boot Pre-Init Helper**: `/usr/local/bin/wavecontroller-hw-init`

---

### Background Service Management (Systemd)

WaveController includes a user-level systemd service that manages background audio routing and mixer state without requiring root privileges:

```bash
# Start background daemon
systemctl --user start wavecontroller.service

# Check daemon status and journal logs
systemctl --user status wavecontroller.service

# Enable automatic start on user login
systemctl --user enable --now wavecontroller.service

# Disable background startup
systemctl --user disable --now wavecontroller.service
```

---

### Hardware Permissions (udev rules)

If you only need to install or update the hardware USB permissions manually:

```bash
curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/data/99-elgato-wave.rules | sudo tee /etc/udev/rules.d/99-elgato-wave.rules > /dev/null && sudo udevadm control --reload-rules && sudo udevadm trigger && echo "Elgato Wave udev rules installed and activated."
```

---

### Running WaveController

Once installed, launch WaveController from your desktop application launcher or run:

```bash
wavecontroller
```

To run exclusively as a background routing daemon (without showing the main mixer window):

```bash
wavecontroller --daemon
```

---

### Documentation & Milestones

- [**Technical Roadmap & Milestones**](ROADMAP.md) - Live tracking of milestones and features on [GitHub Milestones](https://github.com/oparada1988/WaveController/milestones).
- [**Elgato Wave Hardware & PipeWire Architecture**](docs/WaveController_Elgato_Hardware_Technical_Architecture.md) - Reverse-engineered USB memory maps, mode-isolated capacitive muting architecture, and PipeWire graph diagrams.
- [**License**](LICENSE) - WaveController is licensed under the GNU General Public License v3.0 (GPL-3.0).
