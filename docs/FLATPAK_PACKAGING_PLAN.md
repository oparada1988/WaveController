# WaveController Flatpak Packaging Plan & Sandbox Architecture

This document preserves the comprehensive architecture, sandbox permissions, manifest specifications, and Out-Of-Box Experience (OOBE) design for releasing WaveController on Flathub / Flatpak.

---

## 1. Overview & Technical Feasibility

Flatpak packages run inside an isolated Bubblewrap sandbox. Because WaveController coordinates hardware USB control (Elgato devices), PipeWire virtual audio graphs, WirePlumber configurations, and IPC sockets, standard sandboxing requires specific sandbox hole permissions and a one-time host permission setup.

---

## 2. Sandbox Permission Matrix

| Component | Target Path / Resource | Flatpak Finish-Arg | Host Requirement |
| :--- | :--- | :--- | :--- |
| **USB Hardware Control** | `/dev/bus/usb/*` (Elgato Endpoint 0 control transfers via `libusb`) | `--device=all` | **Host Udev Rule**: `/etc/udev/rules.d/99-elgato-wave.rules` (`MODE="0666"`, `TAG+="uaccess"`). |
| **PipeWire Audio Graph** | `$XDG_RUNTIME_DIR/pipewire-0` | `--filesystem=xdg-run/pipewire-0`<br>`--socket=pulseaudio` | Native PipeWire daemon running on host (standard on modern Linux). |
| **WirePlumber Studio Profile** | `~/.config/wireplumber/wireplumber.conf.d/` | `--filesystem=xdg-config/wireplumber:create` | None. WaveController writes `51-wavecontroller-wave-xlr.conf` automatically. |
| **Inter-Process Sockets (IPC)** | `$XDG_RUNTIME_DIR/wavecontroller.sock`<br>`~/.config/WaveController/` | `--filesystem=xdg-run`<br>`--filesystem=xdg-config/WaveController:create` | None. Host plugins (StreamController, VolumeControllerPlus) can read/write shared socket. |
| **Windowing & Display** | Wayland / X11 | `--socket=wayland`<br>`--socket=fallback-x11`<br>`--share=ipc` | None. Standard GTK4 display sockets. |
| **Background Auto-Start** | Session startup on login | XDG Autostart portal or `~/.config/autostart` | None. Writes `com.oparada.WaveController.desktop` with `--daemon`. |

---

## 3. Reference Flatpak Manifest (`com.oparada.WaveController.yml`)

```yaml
app-id: com.oparada.WaveController
runtime: org.gnome.Platform
runtime-version: '46'
sdk: org.gnome.Sdk
command: wavecontroller

finish-args:
  # Display & Windowing
  - --socket=wayland
  - --socket=fallback-x11
  - --share=ipc

  # Audio & PipeWire Graph Access
  - --socket=pulseaudio
  - --filesystem=xdg-run/pipewire-0

  # USB Hardware Access (Elgato Wave:3, Wave XLR, Wave:1, Wave Neo)
  - --device=all

  # Persistent Configuration and IPC Sockets
  - --filesystem=xdg-config/WaveController:create
  - --filesystem=xdg-config/wireplumber:create
  - --filesystem=xdg-run

modules:
  # Native libusb shared library for direct ctypes.CDLL binding
  - name: libusb
    buildsystem: autotools
    sources:
      - type: archive
        url: https://github.com/libusb/libusb/releases/download/v1.0.27/libusb-1.0.27.tar.bz2
        sha256: ffa33d2bfa03a58e6530a6c278772714578b88bbab63b655a6d25243171891ea

  # Python dependencies not bundled in GNOME runtime
  - name: python3-pillow
    buildsystem: simple
    build-commands:
      - pip3 install --verbose --exists-action=i --no-index --find-links="file://${PWD}" --prefix=${FLATPAK_DEST} pillow

  # Application Core
  - name: wavecontroller
    buildsystem: simple
    build-commands:
      - install -Dm755 main.py /app/bin/wavecontroller
      - cp -r wavecontroller assets data /app/
      - install -Dm644 packaging/common/com.oparada.WaveController.desktop /app/share/applications/com.oparada.WaveController.desktop
      - install -Dm644 assets/icons/com.oparada.WaveController.png /app/share/icons/hicolor/512x512/apps/com.oparada.WaveController.png
      - for f in assets/icons/*-symbolic.svg; do [ -f "$f" ] && install -Dm644 "$f" /app/share/icons/hicolor/symbolic/apps/; done
      - for f in assets/icons/*.svg; do [ -f "$f" ] && install -Dm644 "$f" /app/share/icons/hicolor/scalable/apps/; done
    sources:
      - type: dir
        path: ../../
```

---

## 4. OOBE (Out-Of-Box Experience) Requirements for Flatpak

### The Problem
Flatpak cannot write to `/etc/udev/rules.d/` on the host. If a user runs WaveController via Flatpak without having installed the native udev rule first, opening the raw USB interface for an Elgato device returns `LIBUSB_ERROR_ACCESS` (Permission Denied).

### The Solution for Future Implementation
In `wavecontroller/views/setup_wizard.py`:
1. Check if running inside Flatpak:
   ```python
   is_flatpak = os.path.exists("/.flatpak-info")
   ```
2. If `is_flatpak` is `True` and an Elgato device is detected (or selected in Page 5 of OOBE), display a dedicated card or banner:
   - **Header**: "Hardware USB Permissions Required"
   - **Body**: "To allow WaveController to manage gain, Clipguard, and dial LEDs on your Elgato hardware, run this one-time command in your host terminal:"
   - **Command Box with 'Copy' button**:
     ```bash
     curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/data/99-elgato-wave.rules | sudo tee /etc/udev/rules.d/99-elgato-wave.rules && sudo udevadm control --reload-rules && sudo udevadm trigger
     ```
3. If running natively (`is_flatpak == False`), this step remains completely hidden.

---

## 5. Summary & Recommendation

- **Current Priority**: Native distribution via `.deb` and `.rpm` provides seamless out-of-the-box installation with zero host command requirements.
- **Future Flatpak Path**: When revisiting Flatpak, create the manifest under `packaging/flatpak/` and inject the conditional `is_flatpak` banner in the OOBE.
