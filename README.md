<img width="987" height="347" alt="WaveController-banner" src="https://github.com/user-attachments/assets/fcfb7e80-d887-4def-8a87-501ffbf00e78" />

<h2 align="center"><strong>NOTICE: EARLY STAGES OF DEVELOPMENT</strong></h2>

**WaveController** is a modern, native Linux multi-track audio mixing engine, PipeWire compatible, and hardware manager. Designed to provide the complete **Elgato Wave Link**, **Wave Device** tier-1, first class experience on Linux. It also supports other 3rd party USB devices. It is **HEAVILY** developed using Google Antigravity, but heavily tested, and troubleshot by a real human! Currently only supports the Wave XLR(non MK2) and other USB based audio devicess, with the goal to support all Elgato Wave devices, and popular third party hardware.

<p align="center">
  <a href="https://discord.gg/Mhu29PcPJ">
    <img src="assets/discord-server-banner.png" alt="Join the Discord Server" />
  </a>
</p>

<img width="1750" height="1185" alt="Screenshot From 2026-08-25 13-45-00" src="https://github.com/user-attachments/assets/ab713660-c190-4dc0-9c23-c9409d873834" />
<img width="1750" height="1185" alt="Screenshot From 2026-08-25 13-45-06" src="https://github.com/user-attachments/assets/ea92b5b0-ddcb-46d2-90d4-3115ebcae015" />



---

### Features

* **Modular Audio Engine & Regression Protection**:
  * **Decoupled Architecture**: Modularized into specialized subsystems (`engine/graph/process_classifier` and `engine/metering/capture_driver`).
  * **31-Test Audio Invariant Suite**: Automated regression suite guarding zero-bleed audio contracts, submix loopback states, group channel isolation, and binary priority mapping.
  * **Git Pre-Commit Safety Guard**: Automatically blocks regressions on commit.
* **App Group Channels & Virtual System Audio Devices**:
  * **Multi-App Bundling**: Group multiple audio streams (e.g., Discord + Slack + Zoom, or Spotify + Firefox) into a unified channel strip with dynamic stream chips and `[✕]` remove buttons.
  * **Expose as System Audio Device**: Optionally expose any Group Channel as a dedicated virtual PipeWire sink (`WaveController_Channel_<name>`), allowing applications with in-app device selectors (Discord, OBS, games) or Desktop Settings to route directly into the group.
  * **Strict Channel Exclusivity**: Dedicated **Application Channels** provide streamlined 1:1 control, while **Group Channels** handle multi-app bundling and virtual sink exposure.
  * **Instant (<25ms) Zero-Bleed Metering**: Event-driven hooks attach VU taps on the very next frame with zero idle process churn and zero cross-bleed.
* **Clean System Audio Menus & Direct Ingestion**:
  * System settings (Desktop Settings, Discord Output, Browser Menus) display **ONLY** true Output Mixes (*Personal Mix*, *Application Mix*, *Stream Mix*) and explicitly exposed Group Channels.
  * Internal ingestion channels remain purely within WaveController's matrix, eliminating menu clutter.
* **Authoritative App Classification & Zero Collisions**:
  * Prioritizes `application.process.binary` metadata to completely eliminate Chromium/Electron collisions (e.g. Discord, Slack, Teams vs Google Chrome).
* **Studio-Grade Perceptual VU Metering**:
  * Smooth 40 FPS unbuffered PCM stream metering with OBS/Wave Link broadcast $-54\text{ dBFS}$ to $0\text{ dBFS}$ loudness curves.
  * Strict zero-bleed microphone and channel isolation across independent submix faders.
* **Multi-Track Virtual Sub-Mixing**:
  * Virtual Audio Input Channels & App Groups
  * Independent Output Buses
  * Independent Faders & Mute Controls per channel and mix bus.
* **Tier 1 First-Class Elgato Hardware Integration**:
  * Native USB Control Transfers (`wIndex=0x3303`) without needing custom kernel modules.
  * $0\text{--}75\text{dB}$ Preamp Gain Control.
  * 48V Phantom Power toggle for condenser microphones.
  * Enhanced Low-Cut High-Pass Filter (80Hz / 120Hz).
  * Clipguard dual-stage analog/digital limiter.
  * Bi-directional Capacitive Touch Mute synchronization.
  * Hardware Headphone Volume and Mic/PC Crossfade.
  * Gain, Volume, Mute peak on Wave XLR
* **Tier 2 Universal Microphone & Interface Support**:
  * Compatible with any USB microphone (Fifine, Blue Yeti, Rode, Shure, Audio-Technica).
  * Software-emulated Clipguard limiter and Low-Cut filters via PipeWire.
* **GNOME Microphone Icon Bypass**:
  * Real-time VU peak monitoring and helper sub-mix streams are spoofed with `application.id=org.PulseAudio.pavucontrol` and `media.role=volume-control` to prevent GNOME Shell from displaying a persistent orange recording microphone indicator on the top panel.

### Installation & Setup

#### 1. One-Line Remote Installer
Install WaveController, desktop launcher, application icons, and `udev` hardware permissions in a single command:
```bash
curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/scripts/install.sh | bash
```

#### 2. Local Script Installation
Clone the repository and run the management script:
```bash
git clone https://github.com/oparada1988/WaveController.git
cd WaveController
./install.sh
```

---

### Management Script Options (`install.sh`)

The unified installer script supports the following flags:

| Flag / Option | Description |
| :--- | :--- |
| **`./install.sh`** (or `-i`, `--install`) | Installs application to `~/.local/share/wavecontroller`, creates CLI wrapper `~/.local/bin/wavecontroller`, installs desktop icons, and configures `udev` rules. |
| **`./install.sh -u`** (`--upgrade`) | Pulls latest code from Git, syncs the installation, and refreshes icons while **preserving all user configurations**. |
| **`./install.sh -r`** (`--uninstall`) | Cleanly removes the application package, desktop menu entry, icons, and launcher wrapper. |
| **`./install.sh --autostart`** | Enables a systemd user service (`wavecontroller.service`) to start WaveController automatically on desktop login. |
| **`./install.sh --disable-autostart`** | Disables the background systemd startup service. |
| **`./install.sh -h`** (`--help`) | Displays script usage and path information. |

---

### Standard Installation Paths

* **Application Codebase**: `~/.local/share/wavecontroller/`
* **CLI Executable**: `~/.local/bin/wavecontroller`
* **Desktop Application Entry**: `~/.local/share/applications/com.oparada.WaveController.desktop`
* **Desktop Icons**: `~/.local/share/icons/hicolor/512x512/apps/com.oparada.WaveController.png`
* **User Configuration**: `~/.config/WaveController/config.json`
* **Hardware Permissions**: `/etc/udev/rules.d/99-elgato-wave.rules`

---

### Hardware Permissions (udev rules)

If you only need to install or update the hardware USB permissions manually:

```bash
curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/data/99-elgato-wave.rules | sudo tee /etc/udev/rules.d/99-elgato-wave.rules > /dev/null && sudo udevadm control --reload-rules && sudo udevadm trigger && echo "✔ Elgato Wave udev rules successfully installed and activated!"
```

### Documentation & Roadmap

* [**Technical Roadmap & Milestones**](ROADMAP.md) — Live progress and objectives tracked on [GitHub Milestones](https://github.com/oparada1988/WaveController/milestones).
* [**Elgato Wave Hardware & PipeWire Audio Architecture**](docs/WaveController_Elgato_Hardware_Technical_Architecture.md) — In-depth reverse-engineered USB memory maps, mode-isolated capacitive muting architecture, and PipeWire graph diagrams.

---

### Running WaveController

Once installed, launch it directly from your desktop application launcher or run:
```bash
wavecontroller
```
