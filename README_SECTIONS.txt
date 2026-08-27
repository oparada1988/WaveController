## Features

### Hardware Integration & Elgato Wave Protocol
* **Native USB Class Control Transfers**: Direct user-space USB vendor communication (`libusb` endpoint 0, `wIndex=0x3303`) bypassing Linux kernel `snd-usb-audio` interface locking.
* **Elgato Wave Family Support**: First-class hardware management for **Wave XLR**, **Wave:3**, and **Wave:1**.
* **Studio-Grade Microphone Preamp Control**: 0 dB to 75 dB ultra-clean analog gain adjustment in precise 1 dB increments.
* **Hardware 48V Phantom Power Management**: Toggle +48V condenser phantom power with safety confirmation dialogs in the GUI, real-time status indicators, and physical 2-second dial hold hardware synchronization.
* **Analog Clipguard Limiter**: Dual-stage analog compressor/limiter hardware control. Seamlessly diverts clipping input signals through a secondary attenuated (-6 dB) signal path to prevent digital distortion at the hardware ADC level.
* **Enhanced Low-Cut Rumble Filter**: Hardware DSP high-pass filtering (Off, 80 Hz, 120 Hz) to eliminate HVAC hum, desk bumps, and room rumble before audio reaches the OS.
* **Hardware Direct Monitor Mix (Mic / PC Balance)**: Zero-latency hardware sidetone crossfader between microphone input and PC return audio. Includes double-click reset to 50/50 balance.
* **Hardware Headphone Output Amplifier**: Precision volume control (0%–100%) for the onboard low-impedance headphone jack.
* **Rotary Dial & Mode State Synchronization**: 40 Hz bi-directional polling synchronizes physical knob rotation, mode button cycling (Gain -> Headphone -> Balance), and LED states between hardware and desktop GUI.
* **Hardware LED Peak Metering & Ring Personalization**:
  * Customizable 24-bit RGB colors for Mic Gain, Headphone Out, Balance Mode, and Mute ring states.
  * Real-time hardware LED ring visual feedback during audio clipping/peak events.
  * Transient color peeking with automatic 2.0-second ballistics restoration to steady-state mode color.
* **Mode-Isolated Capacitive Muting**: Decouples the physical tap-to-mute sensor based on the active rotary dial setting:
  * **Setting 1 (Gain)**: Mutes Microphone Preamp only (headphone playback stays 100% active).
  * **Setting 2 (Headphone)**: Mutes Headphone Output DAC only (microphone stays live for stream/chat).
  * **Setting 3 (Balance)**: Dual Mute (mutes both Microphone capture and Headphone output simultaneously).

### Virtual Multi-Track Sub-Mixing Engine
* **PipeWire Multi-Mix Routing Matrix**: Create and manage unlimited virtual audio submixes (e.g. Broadcast Mix, Personal Monitor Mix, Chat Mix, Auxiliary Mix).
* **Per-Channel Ingestion Sinks**: Dedicated virtual null-sinks (`WaveController_Channel_{channel_id}`) for applications and input devices.
* **Fast Reactive Stream Interceptor (120ms)**: Real-time PipeWire stream interceptor and native WirePlumber metadata bindings (`pw-metadata -n default <nid> target.object`) eliminate audio bleed and channel switching delays on frame 0.
* **Multi-Device Target Output Selection**: Dynamically route any submix bus to any physical audio interface (Elgato Wave XLR, Fefine USB, onboard motherboard DAC, HDMI, Bluetooth) with live dropdown updating.
* **Dual-Track Stereo Volume Sliders**: Smooth 60 FPS Cairo-rendered stereo faders with Left and Right channel separation, mouse wheel tuning, and double-click unity gain (100%) reset.
* **Real-Time Studio VU Metering**: Dual-channel Left/Right dynamic VU meters with studio ballistics (Emerald Green -> Yellow -> Red) and single-pass GPU/Cairo rendering.
* **Sub-Mix Channel Cell Management**: Independent volume faders, individual mutes, and single-click eject/route buttons for every channel in every submix.
* **GNOME Shell Privacy Indicator Whitelist**: Uses whitelisted media roles (`volume-control` / `pavucontrol`) so real-time VU monitoring operates without triggering persistent desktop recording privacy badges.

### System & App Integrations
* **System Tray Management**: Full DBus `StatusNotifierItem` implementation with quick-access mix volume controls, hardware status, and mute toggles.
* **Stream Deck & Volume Controller Plus IPC**: High-speed Unix domain socket (`/tmp/wavecontroller.sock`) supporting external control from Stream Deck plugins and third-party controllers.
* **Audio Effects & Vocal DSP Rack**: Integrated interface for PipeWire vocal filter-chains (AI Noise Suppression / RNNoise, 3-Band Parametric Equalizer, Broadcast Vocal Compressor, Vocal De-Esser) with persistent state configuration.
* **Portable State Management**: Configuration stored in standard JSON format (`~/.config/WaveController/config.json`) with zero machine-specific hardcoding.


## Installation

### Prerequisites & Dependencies

WaveController requires Python 3.10+, PipeWire, WirePlumber, GTK4, Libadwaita, and libusb.

#### 1. System Packages by Distribution

* **Ubuntu / Debian / Linux Mint / Pop!_OS**:
  ```bash
  sudo apt update
  sudo apt install -y \
      python3 \
      python3-pip \
      python3-gi \
      python3-gi-cairo \
      gir1.2-gtk-4.0 \
      gir1.2-adw-1 \
      libadwaita-1-0 \
      libusb-1.0-0 \
      libusb-1.0-0-dev \
      pipewire \
      wireplumber \
      pipewire-pulse \
      pipewire-alsa \
      curl \
      git
  ```

* **Fedora / RHEL / Nobara**:
  ```bash
  sudo dnf install -y \
      python3 \
      python3-pip \
      python3-gobject \
      gtk4 \
      libadwaita \
      libusb1 \
      libusb1-devel \
      pipewire \
      wireplumber \
      pipewire-pulseaudio \
      curl \
      git
  ```

* **Arch Linux / Manjaro / EndeavourOS**:
  ```bash
  sudo pacman -S --needed \
      python \
      python-pip \
      python-gobject \
      gtk4 \
      libadwaita \
      libusb \
      pipewire \
      wireplumber \
      pipewire-pulse \
      curl \
      git
  ```

#### 2. Python Dependencies
Install required Python packages via pip:
```bash
pip install -r requirements.txt
```
*(Or install `Pillow>=9.0.0` and `PyGObject>=3.44.0` via your system package manager)*

---

### Step-by-Step Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/oparada1988/WaveController.git
   cd WaveController
   ```

2. **Run the installer**:
   ```bash
   chmod +x install.sh
   ./install.sh
   ```

3. **Launch WaveController**:
   Launch it from your desktop application launcher or run:
   ```bash
   wavecontroller
   ```


## Management Script Options

WaveController includes a comprehensive installation and lifecycle management script (`./install.sh`):

| Command Option | Shorthand | Description |
| :--- | :--- | :--- |
| `./install.sh --install` | `-i` | **Default Action**. Installs core application files, desktop menu launcher, system icons, and activates hardware udev rules. |
| `./install.sh --upgrade` | `-u` | Pulls the latest commits from GitHub, updates application files, reloads udev rules, and preserves all user configurations. |
| `./install.sh --uninstall` | `-r` | Terminates active instances, removes executable wrappers, desktop entries, icons, and offers an option to purge or preserve `~/.config/WaveController`. |
| `./install.sh --autostart` | | Installs and enables background desktop startup in `~/.config/autostart/com.oparada.WaveController.desktop`. |
| `./install.sh --disable-autostart` | | Disables background desktop auto-start. |
| `./install.sh --help` | `-h` | Displays the help manual and available flags. |


## Installation Directory

WaveController follows standard XDG Base Directory specifications:

| Component | Filesystem Path | Purpose |
| :--- | :--- | :--- |
| **Application Core** | `~/.local/share/wavecontroller/` | Main application package, modules, and asset bundles. |
| **CLI Wrapper** | `~/.local/bin/wavecontroller` | Executable launcher wrapper added to user `$PATH`. |
| **Desktop Launcher** | `~/.local/share/applications/com.oparada.WaveController.desktop` | Standard Freedesktop application entry for desktop menus. |
| **Application Icons** | `~/.local/share/icons/hicolor/...` | Scalable SVG and Hi-DPI PNG application and tray icons. |
| **User Configuration** | `~/.config/WaveController/config.json` | Matrix states, custom mixes, volume levels, and hardware profiles. |
| **Diagnostic Logs** | `~/.config/WaveController/logs/wavecontroller.log` | Rolling debug logs, hardware transfer logs, and engine diagnostics. |
| **Autostart Entry** | `~/.config/autostart/com.oparada.WaveController.desktop` | Desktop environment login launcher. |
| **Boot Pre-Init Helper** | `/usr/local/bin/wavecontroller-hw-init` | System helper executed on boot to prime USB hardware registers. |
| **Hardware Permissions** | `/etc/udev/rules.d/99-elgato-wave.rules` | Non-root raw USB permissions and systemd-logind `uaccess` rules. |


## udev Rule Installations

To communicate directly with Elgato Wave hardware via raw USB control transfers (`libusb`), the current desktop user must have read/write access to the device character nodes under `/dev/bus/usb/`.

The installer automatically installs and triggers these rules. If you need to install or update the udev rules manually without running the full installer:

### One-Line Installation & Activation
```bash
curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/data/99-elgato-wave.rules | sudo tee /etc/udev/rules.d/99-elgato-wave.rules > /dev/null && sudo udevadm control --reload-rules && sudo udevadm trigger && echo "✔ Elgato Wave udev rules successfully installed and activated!"
```

### Manual Installation From Repository
If you have already cloned the repository:
```bash
sudo cp data/99-elgato-wave.rules /etc/udev/rules.d/99-elgato-wave.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

### Rules Content (`99-elgato-wave.rules`)
```udev
# Elgato Wave XLR, Wave:3, Wave:1 and Audio Hardware USB Permissions
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="007d", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0070", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0088", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0084", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="008f", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0666", TAG+="uaccess"
```

* **`MODE="0666"`**: Grants read/write permissions to the USB node.
* **`TAG+="uaccess"`**: Hands device access dynamically to the logged-in desktop user via `systemd-logind` ACLs without requiring membership in the `audio` or `dialout` groups.
