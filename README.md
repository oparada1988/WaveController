# WaveController

**WaveController** is a modern, native Linux multi-track audio mixing engine and hardware manager designed to provide the complete **Elgato Wave Link** experience on Linux with 1-to-1 integration for **Stream Deck Plus** and **[Volume Controller Plus](https://github.com/oparada1988/Volume-Controller-Plus)**.

![WaveController Screenshot](assets/screenshot.png)

---

### ✨ Features

* **Multi-Track Virtual Sub-Mixing**:
  * 9 Virtual Audio Input Channels (`Microphone`, `Games`, `Music`, `Voice Chat`, `Stream Deck / SFX`, `Browser`, `System Audio`).
  * Dual Independent Output Buses (`Personal Mix` / Headphones and `Record Mix` / Stream Mix for OBS/Discord).
  * Independent Faders & Mute Controls per channel and mix bus.
* **Tier 1 First-Class Elgato Hardware Integration**:
  * Native USB Control Transfers (`wIndex=0x3303`) without needing custom kernel modules.
  * $0\text{--}75\text{dB}$ Preamp Gain Control.
  * 48V Phantom Power toggle for condenser microphones.
  * Enhanced Low-Cut High-Pass Filter (80Hz / 120Hz).
  * Clipguard dual-stage analog/digital limiter.
  * Bi-directional Capacitive Touch Mute synchronization.
  * Hardware Headphone Volume and Mic/PC Crossfade.
* **Tier 2 Universal Microphone & Interface Support**:
  * Compatible with any USB microphone (Fifine, Blue Yeti, Rode, Shure, Audio-Technica).
  * Software-emulated Clipguard limiter and Low-Cut filters via PipeWire.
* **1-to-1 Volume Controller Plus Integration**:
  * Built-in local IPC server at `/tmp/wavecontroller.sock`.
  * Adjust any sub-mix channel directly from Stream Deck Plus physical dials.
  * Live audio VU meters on Stream Deck Plus LCD screens.
* **GNOME Microphone Icon Bypass**:
  * Real-time VU peak monitoring and helper sub-mix streams are spoofed with `application.id=org.PulseAudio.pavucontrol` and `media.role=volume-control` to prevent GNOME Shell from displaying a persistent orange recording microphone indicator on the top panel.

### 📦 Installation & Setup

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

### ⚙️ Management Script Options (`install.sh`)

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

### 📂 Standard Installation Paths

* **Application Codebase**: `~/.local/share/wavecontroller/`
* **CLI Executable**: `~/.local/bin/wavecontroller`
* **Desktop Application Entry**: `~/.local/share/applications/com.oparada.WaveController.desktop`
* **Desktop Icons**: `~/.local/share/icons/hicolor/512x512/apps/com.oparada.WaveController.png`
* **User Configuration**: `~/.config/WaveController/config.json`
* **Hardware Permissions**: `/etc/udev/rules.d/99-elgato-wave.rules`

---

### 🔌 Hardware Permissions (udev rules)

If you only need to install or update the hardware USB permissions manually:

```bash
curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/data/99-elgato-wave.rules | sudo tee /etc/udev/rules.d/99-elgato-wave.rules > /dev/null && sudo udevadm control --reload-rules && sudo udevadm trigger && echo "✔ Elgato Wave udev rules successfully installed and activated!"
```

### 📚 Documentation

For in-depth reverse-engineered USB memory maps, mode-isolated capacitive muting architecture, and PipeWire graph diagrams, review the technical documentation:
* [**Elgato Wave Hardware & PipeWire Audio Architecture**](docs/WaveController_Elgato_Hardware_Technical_Architecture.md)

---

### 🚀 Running WaveController

Once installed, launch it directly from your desktop application launcher or run:
```bash
wavecontroller
```
