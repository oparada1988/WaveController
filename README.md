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

### 🔌 Hardware Permissions (udev rules)

To allow non-root access to Elgato Wave hardware controls (Gain, 48V Phantom Power, LED colors, and Mute sensors), install the udev rules with this one-line command:

```bash
curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/data/99-elgato-wave.rules | sudo tee /etc/udev/rules.d/99-elgato-wave.rules > /dev/null && sudo udevadm control --reload-rules && sudo udevadm trigger && echo "✔ Elgato Wave udev rules successfully installed and activated!"
```

---

### 🚀 Running WaveController

```bash
python3 main.py
```
