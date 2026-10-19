<img width="987" height="347" alt="WaveController-banner" src="https://github.com/user-attachments/assets/fcfb7e80-d887-4def-8a87-501ffbf00e78" />

<h2 align="center"><strong>NOTICE: EARLY STAGES OF DEVELOPMENT</strong></h2>

**WaveController** is a native Linux multi-track audio mixing engine, PipeWire session manager, and hardware controller. It is engineered to deliver the full **Elgato Wave Link** and **Wave Device** tier-1 experience natively on Linux, alongside universal support for third-party USB microphones and audio interfaces.

WaveController is actively developed, human-tested, and audited on production Linux audio workstations. Hardware support currently centers on the **Elgato Wave XLR (MK1)**, with scaffolded support and milestones planned for the Wave XLR MK2, Wave:3, and Wave:1.

<p align="center">
  <a href="https://discord.gg/Mhu29PcPJ">
    <img src="assets/discord-server-banner.png" alt="Join the Discord Server" />
  </a>
</p>

<img width="1750" height="1185" alt="Screenshot From 2026-08-25 13-45-00" src="https://github.com/user-attachments/assets/ab713660-c190-4dc0-9c23-c9409d873834" />
<img width="1750" height="1185" alt="Screenshot From 2026-08-25 13-45-06" src="https://github.com/user-attachments/assets/ea92b5b0-ddcb-46d2-90d4-3115ebcae015" />


<p align="center"><strong>Audio Flow</strong>
<img width="2050" height="1234" alt="Screenshot From 2026-10-19 06-35-16" src="https://github.com/user-attachments/assets/926d0511-aadb-4048-a130-430cf833329c" />

<p align="center"><strong>Compact Mode</strong>
  | Image 1 | Image 2 |
| :---: | :---: |
| ![Vertical](<img width="570" height="473" alt="Screenshot From 2026-10-17 09-17-58" src="https://github.com/user-attachments/assets/889f6103-2616-4acf-a830-df2bd6c4b707" />
) | ![Horizontal](<img width="480" height="510" alt="Screenshot From 2026-10-17 09-18-06" src="https://github.com/user-attachments/assets/e24a5a8a-fe74-4030-8d07-246737e0684d" />
) |

---

### Supported Hardware Devices

WaveController organizes hardware support into two distinct operational tiers:

- **Tier 1 (Native Elgato USB Protocol)**: Direct communication with on-device hardware controllers via raw `libusb` control transfers on Endpoint 0 (`wIndex=0x3303`). This bypasses standard Linux kernel ALSA driver locks, permitting real-time control of analog preamp gain, true 48V phantom power, dual-stage hardware Clipguard limiters, low-cut filters, capacitive touch mute events, and customizable RGB LED rings without interrupting audio capture or playback.
- **Tier 2 (Universal USB Microphones & Audio Interfaces)**: Comprehensive compatibility with any standard USB audio device. Missing hardware features (such as analog limiters or high-pass filters) are emulated transparently in software through PipeWire DSP filter graphs.

| Device | USB VID:PID | Support Tier | Hardware Features | Status |
| :--- | :--- | :--- | :--- | :--- |
| **Elgato Wave XLR (MK1)** | `0fd9:007d` | Tier 1 (Native USB) | 0–75 dB Preamp Gain, 48V Phantom Power, Clipguard Limiter, 80Hz/120Hz Low-Cut, Capacitive Touch Mute, Headphone Volume, Mic/PC Crossfade, RGB LED Ring | **Fully Supported** |
| **Elgato Wave XLR MK2** | `0fd9:00b6` | Tier 1 (Native USB) | 0–75 dB Preamp Gain, 48V Phantom Power, Clipguard Limiter, 80Hz/120Hz Low-Cut, Capacitive Touch Mute, Headphone Volume, Mic/PC Crossfade, RGB LED Ring | Planned (Beta 1) |
| **Elgato Wave:3** | `0fd9:0070` | Tier 1 (Native USB) | 0–40 dB Gain, Clipguard Limiter, 80Hz/120Hz Low-Cut, Capacitive Touch Mute, Headphone Volume, Mic/PC Crossfade, LED Bar Sync | Planned (Beta 1) |
| **Elgato Wave:1** | `0fd9:007c` | Tier 1 (Native USB) | Software Gain, Clipguard Limiter, 80Hz/120Hz Low-Cut, Hardware Dial Mute, Headphone Volume | Planned |
| **Generic USB Microphones & Interfaces** | Any USB Audio Device | Tier 2 (Universal) | Software Gain, Emulated Clipguard Limiter, Emulated Low-Cut Filter, PipeWire Submix Routing, System Mute Sync | **Fully Supported** |

---

### Features

#### 1. Slide-Out FX Inspector Sidebar & Real-Time DSP Rack
- **Animated FX Sidebar**: Smooth 240ms slide-out revealer panel in the mixer matrix with click-away background dismissal.
- **Live DSP Controls**: Real-time intensity sliders (0–100%) and toggle controls for built-in audio processing modules:
  - 10-Band Parametric Equalizer
  - Studio Compressor
  - Noise Gate
  - RNNoise Neural Network Noise Suppression
  - De-Esser
  - Fast Lookahead Peak Limiter
  - High-Pass Filter (80Hz / 120Hz)
- **Rotary Intensity Dials**: Fine-grained rotary dials with neutral center detents, vertical drag, and scroll-wheel nudge support.

#### 2. External Audio Plugin Architecture (VST3 & LV2)
- **Plugin Installation Manager**: Install external VST3 and LV2 plugin bundles via drag-and-drop or directory pickers.
- **Safe Symlink Management**: Plugins are linked into `~/.vst3` and `~/.lv2` and tracked in an installation manifest for clean removal.
- **Custom Scan Directories**: Add custom search paths for DAW-bundled, Flatpak, or system-wide audio plugin folders.
- **Deduplicated Plugin Index**: External plugin browser isolates user-installed plugins from internal system duplicates.

#### 3. Unix IPC Socket API & Stream Deck / StreamController Integration
- **High-Performance Unix Domain Socket**: Local control socket located at `~/.config/WaveController/ipc.sock`.
- **Granular DSP Commands**: Supports `get_fx_status`, `toggle_fx` (master rack bypass), `toggle_channel_effect`, and `set_channel_effect`.
- **Hardware & UI Parity**: Bi-directional synchronization between Stream Deck keys, rotary dials, hardware buttons, and GUI state.

#### 4. Virtual System Output Sinks & Desktop Audio
- **Direct Sound Card Exposure**: Single application channels can be exposed as independent virtual PipeWire sound cards (`media.class="Audio/Sink"`), enabling discrete routing inside external software (OBS, Discord, games) without requiring a group channel.
- **Desktop Audio Default Sink**: Dedicated virtual sink channel that automatically captures and controls unassigned desktop audio streams.

#### 5. Application Group Channels & Direct Matrix Ingestion
- **Multi-App Bundling**: Combine multiple audio streams (e.g., Discord + Slack + Zoom, or Spotify + Firefox) into a single fader strip with removable stream chips.
- **Clean Audio Menus**: Internal channel ingestion sinks stay contained within WaveController, preventing menu clutter in system settings.

#### 6. Real-Time Stream Routing & Binary Classification
- **Instant Stream Reconcile**: Event-driven `pw-mon` stream monitor immediately routes newly launched applications or browser tabs with zero audible lag.
- **Authoritative Process Classification**: Inspects `application.process.binary` metadata to eliminate collisions between Chromium and Electron applications.

#### 7. Studio-Grade Perceptual VU Metering
- **Broadcast-Calibrated Ballistics**: 40 FPS unbuffered PCM stream metering with calibrated -54 dBFS to 0 dBFS loudness curves.
- **Zero-Bleed Isolation**: Strict channel and submix separation prevents cross-talk across independent monitoring faders.

#### 8. Multi-Track Virtual Sub-Mixing
- **Discrete Output Buses**: Manage independent mixes (Personal Mix, Stream Mix, Application Mix) with discrete per-channel faders and mute states.
- **Master Bus Headers**: Output cards provide hardware assignment, volume sliders, mute toggles, and direct monitor routing.

#### 9. Hardware Disconnect Shield & Power Lifecycle Protection
- **Disconnect Shield**: Prevents physical Wave devices from falsely reporting as offline during dynamic PipeWire profile switches.
- **Resume Protection**: 64-bit ctypes libusb signatures prevent native segmentation faults across system sleep cycles.
- **Resume Mute Shield & Blackout**: Automatically turns off hardware LEDs during sleep and silences hardware outputs on wake until USB registers are fully synchronized, eliminating clicks, pops, and volume spikes.

#### 10. GNOME Panel Recording Indicator Bypass
- Metering taps and internal helper loopbacks report with `application.id=org.PulseAudio.pavucontrol` and `media.role=volume-control`, preventing GNOME Shell from displaying a persistent recording microphone pill on the top panel.

#### 11. Modular Audio Engine & Regression Invariants
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
- **IPC Socket**: `~/.config/WaveController/ipc.sock`
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
