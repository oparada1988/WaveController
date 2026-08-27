# WaveController — Elgato Wave Hardware & PipeWire Audio Architecture
**Technical Specification, Reverse-Engineered USB Vendor Protocols, Hardware DSP Features, LED Peak Metering, Multi-Mix Routing Engine & `udev` Permissions**

---

## 1. Executive Overview & System Topology

WaveController delivers native, hardware-level integration for the **Elgato Wave product family** (Wave XLR, Wave:3, and Wave:1) on Linux. The system combines low-level USB vendor control transfers with modern PipeWire DSP submixing to achieve real-time volume management, mode-isolated capacitive muting, customizable LED rings with peak warnings, and sub-millisecond audio routing.

```mermaid
flowchart TB
    subgraph HW["Elgato Wave XLR / Wave:3 Physical Hardware"]
        Dial["Rotary Dial (Click to Cycle Modes)"]
        Sensor["Capacitive Mute Sensor (Top Plate)"]
        PwrHold["2-Second Dial Push (48V Toggle)"]
        Ring["Multi-Color LED Ring, Peak Meter & Mode Indicators"]
    end

    subgraph USB_Layer["Low-Level USB Vendor Engine (elgato_wave.py)"]
        Libusb["libusb Control Transfers (0x0FD9:0x007D / 0x0063 / 0x0070)"]
        Poller["40 Hz Non-Blocking Hardware Polling Loop"]
        LEDTrans["LED Color Engine, Peak Feedback & Transient Restorer"]
    end

    subgraph Core_Engine["WaveController Core Subsystem"]
        USBHW["USBHardwareManager (usb_hardware.py)"]
        PW["PipeWireManager (pipewire_manager.py)"]
        CFG["ConfigManager (~/.config/WaveController/config.json)"]
    end

    subgraph PW_Graph["PipeWire Audio Graph (Real-Time Sub-Mixing)"]
        MicIn["Physical Wave XLR Mic In (ALSA Node)"]
        ChatSource["WaveController_chat_mix_Source"]
        NullSinks["Virtual Mix Sinks (Broadcast, Personal, Custom)"]
        HeadphoneDAC["Physical Wave XLR Headphone DAC (ALSA Playback)"]
    end

    HW <-->|"USB Vendor Protocol (wIndex=0x3303)"| Libusb
    Libusb --> Poller
    Poller --> USBHW
    USBHW <--> CFG
    USBHW <--> PW
    PW <--> PW_Graph
```

---

## 2. Low-Level Elgato USB Vendor Protocol

The Elgato Wave family communicates over USB Audio Class (UAC2) for PCM audio streams and custom USB Vendor Control transfers for hardware parameters.

### 2.1 Supported Hardware Profiles
| Device Name | Vendor ID (`VID`) | Product ID (`PID`) | Interface | Endpoint / `wIndex` | Max Gain |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Elgato Wave XLR** | `0x0FD9` | `0x007D` | Interface 3 | `0x3303` | **75 dB** (Ultra-Low-Noise Preamp) |
| **Elgato Wave:3** | `0x0FD9` | `0x0063` | Interface 3 | `0x3303` | **40 dB** (Internal Condenser Capsule) |
| **Elgato Wave:1** | `0x0FD9` | `0x0070` | Interface 3 | `0x3303` | **40 dB** (Internal Condenser Capsule) |

### 2.2 USB Control Transfer Parameters
* **Read Request (`RT_CLASS_IN`)**: `bmRequestType = 0xA1`, `bRequest = 0x01`
* **Write Request (`RT_CLASS_OUT`)**: `bmRequestType = 0x21`, `bRequest = 0x09`
* **`wValue` (Config Buffer)**: `0x0100` (Configuration packet payload)
* **`wValue` (Device Info)**: `0x0200` (Firmware version, serial string, API version)
* **Packet Size**: 34 Bytes (`0x22`)

### 2.3 34-Byte Hardware Configuration Memory Layout

```
Offset (Hex/Dec)   Type      Field Description & Bitmask
------------------------------------------------------------------------------------------------------
0x00 (0)           uint8     Report ID (Fixed 0x01)
0x01..0x03 (1..3)  uint8[3]  Header / Reserved (0x00, 0x00, 0x00)
0x04 (4)           uint8     Hardware Mute Register (0x00 = Unmuted, 0x01 = Hardware Preamp Muted)
0x05 (5)           uint8     Reserved
0x06..0x07 (6..7)  uint16_LE Microphone Preamp Gain Raw (0x0000 = 0 dB, 0x002B..0xFFFF = Up to 75 dB)
0x08 (8)           uint8     Headphone Monitor Volume (0x00 = 0%, 0x64 = 100%)
0x09 (9)           uint8     Mic/PC Sidetone Crossfade Balance (0x00 = 100% Mic, 0x64 = 100% PC Audio)
0x0A (10)          uint8     48V Phantom Power Toggle (0x00 = OFF, 0x01 = ON)
0x0B (11)          uint8     Enhanced Low-Cut Filter (0x00 = OFF, 0x01 = 80 Hz, 0x02 = 120 Hz)
0x0C (12)          uint8     Analog Clipguard Compressor (0x00 = OFF, 0x01 = ON)
0x0D (13)          uint8     Reserved
0x0E (14)          uint8     Rotary Dial Active Mode (0x01 = Gain, 0x02 = Headphone Out, 0x03 = Balance)
0x0F (15)          uint8     Reserved
0x10..0x12 (16..18) uint8[3] Gain Mode LED Color Bank (R, G, B: 0..255)
0x13..0x15 (19..21) uint8[3] Headphone Mode LED Color Bank (R, G, B: 0..255)
0x16..0x18 (22..24) uint8[3] Sidetone Balance LED Color Bank (R, G, B: 0..255)
0x19..0x1B (25..27) uint8[3] Mute Indicator Ring Color Bank (R, G, B: 0..255)
0x1C..0x21 (28..33) uint8[6] Reserved / Checksum Padding
```

---

## 3. Comprehensive Hardware Features & Analog DSP Architecture

### 3.1 Ultra-Low-Noise Microphone Preamp & 75 dB Gain Architecture
* **Dynamic Range & Step Scaling**: The Elgato Wave XLR features a custom analog preamp delivering up to **75 dB of ultra-clean gain**, eliminating the need for inline booster preamps (e.g. Cloudlifter, FetHead) even when driving low-sensitivity dynamic broadcast microphones (e.g. Shure SM7B, Electro-Voice RE20).
* **Digital-to-Analog Word Mapping**: In `elgato_wave.py`, gain is represented as a 16-bit little-endian integer (`off_gain = 6`). The raw value maps linearly between `0x0000` ($0\text{ dB}$) and `0x4B00` ($75\text{ dB}$) for Wave XLR, or `0x2800` ($40\text{ dB}$) for Wave:3:
  $$\text{Gain}_{\text{raw}} = \text{round}\left(\frac{\text{Gain}_{\text{dB}}}{\text{Gain}_{\text{max}}} \times \text{Raw}_{\text{max}}\right)$$
* **Bidirectional Rotary Knob Synchronization**: Rotating the physical dial updates `off_gain` directly in the hardware memory buffer; WaveController's 40 Hz polling thread reads the change, calculates the decibel gain to 1 decimal place, and immediately updates the GUI fader and PipeWire digital ingestion gain.

### 3.2 48V Condenser Phantom Power & Dual-Action Interlocks
* **Capacitive Studio Power Delivery**: Supplies true 48-volt DC phantom power across XLR pins 2 and 3 to power studio condenser microphones (e.g. Neumann U87, Rode NT1, Audio-Technica AT2020).
* **Hardware Long-Press Trigger**: Holding down the Wave XLR physical rotary dial for **2.0 seconds** toggles the phantom power state. WaveController tracks this interaction, updates internal states, and reflects the status on the GUI "48V" badge.
* **Software Safety Confirmation Interlock**: Toggling 48V through the desktop GUI presents an intentional safety modal preventing accidental voltage spikes to delicate vintage ribbon microphones.

### 3.3 Analog Clipguard Limiter & Parallel Ingestion Safety Path
* **Dual-Stage Analog Circuitry**: Rather than relying on digital software brickwall limiters that degrade audio resolution after clipping occurs, Clipguard operates in the **analog domain before the Analog-to-Digital Converter (ADC)**.
* **Parallel Safety Stream**: When enabled (`off_clipguard = 12`, byte `0x01`), the hardware runs a parallel second analog signal path attenuated by $-6\text{ dB}$. If the primary input signal spikes into analog saturation or clipping, the hardware instantaneously and seamlessly crossfades to the attenuated signal path, ensuring zero digital clipping distortion at the OS level.

### 3.4 Dual-Frequency Enhanced Low-Cut High-Pass Filter (80 Hz & 120 Hz)
* **Hardware DSP Filtering**: Low-cut filtering (`off_low_cut = 11`) executes directly within the Wave DSP pipeline before PCM encoding.
* **Frequency Stepping**:
  * `0x00` — **Bypass / Flat**: Full-range frequency response (20 Hz – 20 kHz).
  * `0x01` — **80 Hz High-Pass**: Attenuates sub-bass room rumble, mechanical keyboard thumps, and desk handling noise while preserving vocal warmth.
  * `0x02` — **120 Hz High-Pass**: Aggressively eliminates HVAC air conditioning rumble, heavy traffic vibrations, and severe environmental proximity effect.

### 3.5 Zero-Latency Direct Monitor Mix & Sidetone Crossfade
* **Hardware Sidetone Crossfader**: Register `off_monitor_mix = 9` controls the analog mixing balance between the direct microphone capture and the PC audio return stream:
  * `0x00` ($0\%$): $100\%$ Microphone Direct Monitor (zero-latency sidetone).
  * `0x32` ($50\%$): $50/50$ Balance (Microphone and PC audio mixed equally).
  * `0x64` ($100\%$): $100\%$ PC Audio Return (Microphone sidetone muted in headphones).
* **Desktop Quick Reset**: Double-clicking the Direct Monitor Balance fader in the WaveController header immediately resets the hardware register to `50%` ($50/50$).

### 3.6 Hardware Headphone DAC & Low-Impedance Drive
* **High-Power Headphone Output**: Register `off_hp_vol = 8` governs the hardware headphone amplifier (0–100%).
* **Low-Impedance Compatibility**: Wave XLR supports an ultra-low output impedance mode designed to drive demanding planar magnetic or low-impedance in-ear monitors (IEMs) without frequency damping distortion.

---

## 4. Hardware LED Ring Architecture & Peak Metering

### 4.1 24-Bit RGB Register Bank
WaveController provides full RGB personalization for the hardware multi-LED ring on the front of the Wave XLR and Wave:3:
* **Gain Mode Ring Color (`0x10..0x12`)**: Color displayed when rotary dial adjusts mic gain.
* **Headphone Mode Ring Color (`0x13..0x15`)**: Color displayed when dial adjusts headphone volume.
* **Balance Mode Ring Color (`0x16..0x18`)**: Color displayed when dial adjusts Mic/PC crossfade balance.
* **Mute State Ring Color (`0x19..0x1B`)**: Color displayed when capacitive mute is engaged.

### 4.2 Hardware LED Peak & Visual Clipping Indicator
* **Analog Peak Saturation Warning**: In addition to steady-state mode illumination, the Elgato Wave hardware LED ring provides physical visual feedback when input signals exceed safe headroom thresholds ($>-1\text{ dBFS}$).
* **Hardware Clipguard Visual Alert**: When input audio levels spike and Clipguard engages its secondary attenuated analog path, the physical LED ring pulses/flashes **Vivid Red** to alert the speaker that their voice is peaking and entering compression.
* **Transient Color Peeking with 2.0-Second Ballistics**:
  * When a user changes the volume of an inactive mode (e.g. changing Headphone volume via software while the physical knob is set to Gain mode), WaveController illuminates the target mode's color on the hardware ring for **2.0 seconds** so the user visually confirms the target level.
  * After 2.0 seconds, the hardware ring automatically reverts with smooth ballistics to the steady-state color of the physical knob's active mode.
* **Software Matrix VU vs Hardware Register Protection**:
  * Software matrix meters stream continuous Left/Right stereo audio peaks at **60 FPS** using Cairo-rendered gradients (Emerald Green $\rightarrow$ Amber $\rightarrow$ Red).
  * The hardware USB control transfers protect internal EEPROM and USB endpoints by updating steady-state registers and peak alert states dynamically without bus-saturating continuous writes.

---

## 5. Mode-Isolated Capacitive Muting Architecture

In factory Elgato firmware, touching the top capacitive plate always sends a UAC2 hardware capture mute packet to the host operating system. WaveController decouples this behavior in software to provide **context-sensitive, dynamic muting based on the active rotary dial setting**.

```mermaid
flowchart TD
    Touch["Capacitive Mute Sensor Tapped"] --> Detect["Poller detects changed mute byte in cfg:4"]
    Detect --> ModeCheck{"Active Dial Mode cfg:14"}

    ModeCheck -->|"Setting 1: LED 1 Gain / Mic"| S1["Mute Microphone Preamp"]
    S1 --> S1_1["PipeWireManager.set_channel_master_mute: elgato_wave_xlr, True"]
    S1_1 --> S1_2["Sever Mic capture links in PipeWire Graph"]
    S1_2 --> S1_3["Headphone Output Mix remains 100% ACTIVE"]

    ModeCheck -->|"Setting 2: LED 2 Headphone Out"| S2["Mute Headphone Output Mix"]
    S2 --> S2_1["PipeWireManager.set_mix_master_mute: personal_mix / beta, True"]
    S2_1 --> S2_2["pw-link -d monitor_FL/FR from Wave XLR DAC"]
    S2_2 --> S2_3["UAC2 Shield: wpctl set-mute default_source 0"]
    S2_3 --> S2_4["Microphone Preamp & Chat Mix remain 100% ACTIVE"]

    ModeCheck -->|"Setting 3: LED 3 Balance / Mix"| S3["Dual Mute: Simultaneously Mute Mic & Headphone Mix"]
    S3 --> S3_1["Sever Mic capture stream AND unbind Headphone DAC monitor link"]
```

### 5.1 Mode Isolation Matrix
| Hardware Dial LED | Active Mode | Hardware Tap-to-Mute Action | Software Audio State |
| :--- | :--- | :--- | :--- |
| **LED 1 (Mic Icon)** | `gain` | Mutes **Microphone Only** (`elgato_wave_xlr` / `mic`). | Stream/Discord muted; Spotify and game audio in headphones stay $100\%$ active. |
| **LED 2 (Headphone Icon)** | `hp` | Mutes **Headphone Output Mix Only** (`beta` / `personal_mix`). | Headphone audio silenced instantly with $0\text{ms}$ latency; Mic and Chat Mix stay active. |
| **LED 3 (Balance Icon)** | `mix` | **Dual Mute**: Mutes **both** Microphone and Headphone Output Mix. | Complete broadcast and personal monitoring silence. |

### 5.2 Linux Kernel UAC2 Shielding Mechanism
When tapping capacitive mute while on **Setting 2 (Headphone mode)**, the Linux ALSA driver automatically intercepts the UAC2 HID packet and marks `@DEFAULT_AUDIO_SOURCE@` as `[MUTED]`.
WaveController neutralizes this side-effect:
1. `USBHardwareManager` immediately dispatches `wpctl set-mute @DEFAULT_AUDIO_SOURCE@ 0` to un-clamp ALSA.
2. `PipeWireManager` skips generic ALSA sync by identifying the active Elgato hardware profile (`is_elgato == True`).
3. Software keeps `channel_master_states["elgato_wave_xlr"]["muted"] = False` so live voice capture is uninterrupted.

---

## 6. PipeWire Graph, Multi-Track Virtual Routing & Fast Stream Interceptor

WaveController bypasses traditional single-sink desktop limitations by provisioning virtual PipeWire submixes, custom loopbacks, and dynamic port linkers.

```mermaid
graph LR
    subgraph Inputs["Audio Applications & Capture"]
        Spotify["Spotify Audio Stream"]
        Game["Game Audio Stream"]
        Discord["Discord Voice Stream"]
        WaveMic["Physical Wave XLR Mic In"]
    end

    subgraph Submixes["Virtual Matrix Loopbacks (pw-loopback)"]
        Sub_Spot_Beta["Submix: Spotify -> beta (5ms latency)"]
        Sub_Spot_Mobo["Submix: Spotify -> mobo mix (5ms latency)"]
        Sub_Game_Beta["Submix: Game -> beta (5ms latency)"]
        Sub_Mic_Chat["Submix: Wave Mic -> Chat Mix"]
    end

    subgraph Sinks["PipeWire Virtual Null Sinks"]
        Sink_Beta["WaveController_personal_mix_Sink (beta)"]
        Sink_Mobo["WaveController_mobo_mix_Sink (mobo mix)"]
        Source_Chat["WaveController_chat_mix_Source (Broadcast Mic)"]
    end

    subgraph Outputs["Physical Output Endpoints"]
        ElgatoDAC["alsa_output.usb-Elgato_Wave_XLR... (Headphones)"]
        MoboDAC["alsa_output.pci-0000_14_00.4... (Speakers)"]
        OBS["OBS Studio / Discord Input Stream"]
    end

    Spotify --> Sub_Spot_Beta --> Sink_Beta
    Spotify --> Sub_Spot_Mobo --> Sink_Mobo
    Game --> Sub_Game_Beta --> Sink_Beta
    WaveMic --> Sub_Mic_Chat --> Source_Chat

    Sink_Beta -->|"pw-link monitor_FL/FR (Severed on Mute)"| ElgatoDAC
    Sink_Mobo -->|"pw-link monitor_FL/FR"| MoboDAC
    Source_Chat --> OBS
```

### 6.1 Real-Time Monitor Mix Severing
When a user mutes a mix (e.g. `personal_mix` / `beta`):
1. `_sync_mix_physical_output_routing()` detects `is_mix_muted == True`.
2. PipeWire executes:
   ```bash
   pw-link -d WaveController_personal_mix_Sink:monitor_FL alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR...:playback_FL
   pw-link -d WaveController_personal_mix_Sink:monitor_FR alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR...:playback_FR
   ```
3. Audio ceases to flow to the headphone DAC immediately.
4. When unmuted, `pw-link` reattaches the monitor ports in $<1\text{ms}$ with smooth audio resumption.

### 6.2 120ms Fast Reactive Stream Interceptor & Native WirePlumber Metadata Bindings
To eliminate audio bleed and delays when launching applications or switching mix channels:
1. **Ultra-Fast Stream Poller (`_reconcile_app_streams_fast`)**: Runs every ~120ms (3 ticks). It inspects active candidate ports via `pw-link -o` in ~5ms.
2. **WirePlumber Metadata Steering**: The moment an assigned application connects, WaveController binds it directly to its dedicated ingestion channel sink:
   ```bash
   pw-metadata -n default <stream_node_id> target.object WaveController_Channel_{channel_id}
   ```
3. WirePlumber commits the target to `~/.local/state/wireplumber/stream-properties`. All future audio frames route to the designated virtual sink on **frame 0**, completely preventing any temporary audio bleed into default sinks.
4. Any leaking links to physical outputs (`alsa_output.`) or alternate mix sinks are instantly unlinked via `pw-link -d`.

### 6.3 GNOME Shell Microphone Privacy Indicator Bypass
GNOME Shell and Linux desktop privacy monitors track active `Stream/Input/Audio` capture nodes and display a persistent orange microphone recording icon on the top panel whenever an application reads audio input.

To allow real-time VU peak metering and sub-mix monitoring without triggering persistent privacy alerts:
1. All `pw-record` and `pw-loopback` streams are registered with the following PipeWire node properties:
   * `application.id = "org.PulseAudio.pavucontrol"`
   * `application.name = "pavucontrol"`
   * `application.icon_name = "pavucontrol"`
   * `application.process.binary = "pavucontrol"`
   * `media.role = "volume-control"`
2. Since `org.PulseAudio.pavucontrol` and `volume-control` media roles are explicitly whitelisted in GNOME Shell's audio privacy monitoring exclusions, WaveController captures live audio peaks and performs matrix sub-mixing cleanly without displaying unwanted system recording notifications.

---

## 7. Persistence & Multi-Machine Portability

All hardware configurations are $100\%$ portable across any Linux distribution running PipeWire:

1. **Hardware State Schema (`~/.config/WaveController/config.json`)**:
   ```json
   {
     "hardware_settings": {
       "gain_db": 58,
       "phantom_power": true,
       "clipguard": true,
       "low_cut": "80Hz",
       "led_colors": {
         "gain": "#FFFFFF",
         "hp": "#2ECC71",
         "mix": "#FF9500",
         "mute": "#FF0000"
       }
     }
   }
   ```
2. **Zero Hardcoded Machine IDs**:
   * Hardware binding queries USB descriptors and ALSA udev properties dynamically.
   * Device swapping or migrating to new hardware automatically rebinds existing mixer matrix configurations.

---

## 8. Linux USB Permissions & `udev` Rules Architecture

On modern Linux systems, direct USB hardware control via raw control transfers (`libusb_control_transfer`) is restricted to `root` users by default. Standard non-root user processes cannot claim interfaces or write configuration packets to `/dev/bus/usb/*` without explicit permissions.

### 8.1 Anatomy of `99-elgato-wave.rules`
The udev rules file (`data/99-elgato-wave.rules`) grants seamless non-root permissions to the logged-in desktop user:

```udev
# Elgato Wave XLR, Wave:3, Wave:1 and Audio Hardware USB Permissions
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="007d", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0070", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0088", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0084", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="008f", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0666", TAG+="uaccess"
```

### 8.2 Key Attribute Directives Explained
* **`SUBSYSTEM=="usb"`**: Restricts matching to the Linux USB kernel subsystem (`/dev/bus/usb/BBB/DDD`).
* **`ATTRS{idVendor}=="0fd9"`**: Targets all hardware manufactured by **Elgato Systems GmbH**.
* **`ATTRS{idProduct}=="007d"`**: Matches specific hardware models (e.g. `0x007D` for Wave XLR, `0x0070` for Wave:1, `0x0088` / `0x0084` for Wave:3).
* **`MODE="0666"`**: Grants read and write permissions (`rw-rw-rw-`) to the underlying character device node.
* **`TAG+="uaccess"`**: Automatically adds the device node to `systemd-logind`'s dynamic Access Control List (ACL). This assigns hardware ownership directly to the currently active graphical desktop session user without requiring manual user group additions (such as adding the user to the `audio` or `dialout` groups).

### 8.3 Installation and Activation Protocol
To install and trigger the rules without rebooting:
```bash
curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/data/99-elgato-wave.rules | sudo tee /etc/udev/rules.d/99-elgato-wave.rules > /dev/null && sudo udevadm control --reload-rules && sudo udevadm trigger && echo "✔ Elgato Wave udev rules successfully installed and activated!"
```

---

*Document compiled and verified against WaveController v0.0.1.1 (Alpha 1, Pre-Alpha 1).*
