import os
import json
import socket
import threading

CONFIG_SOCKET_PATH = os.path.expanduser("~/.config/WaveController/wavecontroller.sock")
USER_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
USER_SOCKET_PATH = os.path.join(USER_RUNTIME_DIR, "wavecontroller.sock")
TMP_SOCKET_PATH = "/tmp/wavecontroller.sock"

class IPCServer:
    """
    Unix Domain Socket IPC Server allowing WaveController Plugin and StreamController
    to interact directly with WaveController sub-mixes, faders, and meters.
    """
    def __init__(self, pipewire_mgr, peak_monitor, hardware_mgr):
        self.pipewire_mgr = pipewire_mgr
        self.peak_monitor = peak_monitor
        self.hardware_mgr = hardware_mgr
        self.running = False
        self.server_sock = None
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run_server, daemon=True)
        self.thread.start()

    def _clean_sockets(self):
        for path in [CONFIG_SOCKET_PATH, USER_SOCKET_PATH, TMP_SOCKET_PATH]:
            if os.path.exists(path) or os.path.islink(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    def stop(self):
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        self._clean_sockets()

    def _run_server(self):
        self._clean_sockets()

        os.makedirs(os.path.dirname(CONFIG_SOCKET_PATH), exist_ok=True)

        bound_path = None
        # Prioritize CONFIG_SOCKET_PATH for Flatpak access
        for path in [CONFIG_SOCKET_PATH, USER_SOCKET_PATH, TMP_SOCKET_PATH]:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.bind(path)
                sock.listen(10)
                sock.settimeout(1.0)
                self.server_sock = sock
                bound_path = path
                break
            except Exception:
                continue

        if not bound_path:
            return

        # Symlink other paths to the bound socket path for host & flatpak interop
        for path in [CONFIG_SOCKET_PATH, USER_SOCKET_PATH, TMP_SOCKET_PATH]:
            if path != bound_path and not os.path.exists(path) and not os.path.islink(path):
                try:
                    os.symlink(bound_path, path)
                except Exception:
                    pass

        while self.running:
            try:
                conn, _ = self.server_sock.accept()
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _match_channel_id(self, target: str) -> str:
        if not target:
            return "mic"
        target_low = str(target).lower().strip()
        with self.pipewire_mgr._lock:
            # 1. Exact ID match
            for c in self.pipewire_mgr.channels:
                if c["id"].lower() == target_low or c["name"].lower() == target_low:
                    return c["id"]
            # 2. Assigned apps match
            for c in self.pipewire_mgr.channels:
                assigned = self.pipewire_mgr.assigned_apps.get(c["id"], [])
                for a in assigned:
                    if target_low in a.lower() or a.lower() in target_low:
                        return c["id"]
            # 3. Fuzzy name match
            for c in self.pipewire_mgr.channels:
                if target_low in c["name"].lower() or c["name"].lower() in target_low:
                    return c["id"]
        return target_low

    def _match_mix_id(self, target: str) -> str:
        if not target:
            with self.pipewire_mgr._lock:
                return self.pipewire_mgr.mixes[0]["id"] if self.pipewire_mgr.mixes else "personal_mix"
        target_low = str(target).lower().strip()
        with self.pipewire_mgr._lock:
            # 1. Exact match
            for m in self.pipewire_mgr.mixes:
                if m["id"].lower() == target_low or m["name"].lower() == target_low:
                    return m["id"]
            # 2. Suffix/prefix match (e.g. "personal" -> "personal_mix")
            for m in self.pipewire_mgr.mixes:
                m_id_low = m["id"].lower()
                m_name_low = m["name"].lower()
                if target_low in m_id_low or m_id_low in target_low:
                    return m["id"]
                if target_low in m_name_low or m_name_low in target_low:
                    return m["id"]
        return target_low

    def _is_hardware_connected(self) -> bool:
        if not self.hardware_mgr:
            return False
        if hasattr(self.hardware_mgr, "is_connected"):
            conn = self.hardware_mgr.is_connected
            return bool(conn() if callable(conn) else conn)
        info = self.hardware_mgr.get_elgato_device_info() if hasattr(self.hardware_mgr, "get_elgato_device_info") else {}
        if info:
            return bool(info.get("connected", False))
        return bool(getattr(self.hardware_mgr, "device_name", ""))

    def _is_wave_channel(self, ch_id: str) -> bool:
        if not self._is_hardware_connected():
            return False
        c = str(ch_id).lower()
        if c in ("mic", "elgato_wave_xlr") or c.startswith("elgato_wave") or "wave_xlr" in c or "wave_3" in c or "wave_1" in c or "wave_neo" in c:
            return True
        ch_info = self.pipewire_mgr.get_channel_info(ch_id) if hasattr(self.pipewire_mgr, "get_channel_info") else None
        if ch_info and ch_info.get("type") in ("source", "hardware"):
            n = ch_info.get("name", "").lower()
            if "elgato" in n or "wave_xlr" in n or "wave:3" in n or "wave:1" in n or "wave neo" in n or "0fd9" in n:
                return True
        return False

    def _is_wave_mix(self, mix_id: str) -> bool:
        if not self._is_hardware_connected():
            return False
        mix_info = next((m for m in self.pipewire_mgr.mixes if m.get("id") == mix_id), None)
        if not mix_info:
            for m in self.pipewire_mgr.mixes:
                if self._match_mix_id(m.get("id")) == mix_id:
                    mix_info = m
                    break
        target = str(mix_info.get("target_device", "") if mix_info else "").lower()
        return "wave" in target or "elgato" in target or "personal" in str(mix_id).lower() or target in ("default", "")

    def _process_command(self, req: dict) -> dict:
        cmd = req.get("command")
        res = {"status": "ok"}

        if cmd == "get_channels":
            channels_list = []
            for c in self.pipewire_mgr.channels:
                c_data = dict(c)
                ch_id = str(c_data.get("id", "")).lower()
                if ch_id in ("mic", "microphone"):
                    if self._is_hardware_connected():
                        dev_icon = getattr(self.hardware_mgr, "get_device_icon", lambda *a: None)(self.hardware_mgr.device_name)
                        if dev_icon and dev_icon not in ("audio-input-microphone-symbolic", "network-offline-symbolic"):
                            c_data["icon"] = dev_icon
                channels_list.append(c_data)
            res["channels"] = channels_list
            res["mixes"] = self.pipewire_mgr.mixes
            res["states"] = self.pipewire_mgr.channel_states
            res["master_states"] = self.pipewire_mgr.channel_master_states
            res["mix_states"] = self.pipewire_mgr.mix_states
            res["assigned_apps"] = self.pipewire_mgr.assigned_apps
        elif cmd == "get_volume":
            raw_target = req.get("channel_id") or req.get("target") or "mic"
            ch = self._match_channel_id(raw_target)
            mx = self._match_mix_id(req.get("mix_id")) if req.get("mix_id") else None
            if mx:
                res["state"] = self.pipewire_mgr.get_channel_state(ch, mx)
            else:
                res["state"] = {
                    "volume": self.pipewire_mgr.get_channel_master_volume(ch),
                    "muted": self.pipewire_mgr.get_channel_master_mute(ch)
                }
        elif cmd in ["set_volume", "sync_volume"]:
            raw_target = req.get("channel_id") or req.get("target") or req.get("app_name") or "mic"
            ch = self._match_channel_id(raw_target)
            mx = self._match_mix_id(req.get("mix_id")) if req.get("mix_id") else None
            vol = req.get("volume")
            muted = req.get("muted")
            
            if mx:
                if vol is not None:
                    self.pipewire_mgr.set_channel_volume(ch, mx, int(vol))
                if muted is not None:
                    self.pipewire_mgr.set_channel_mute(ch, mx, bool(muted))
                res["state"] = self.pipewire_mgr.get_channel_state(ch, mx)
            else:
                if vol is not None:
                    self.pipewire_mgr.set_channel_master_volume(ch, int(vol))
                    if self._is_wave_channel(ch):
                        gain_db = int(round((int(vol) / 100.0) * 75.0))
                        self.hardware_mgr.set_gain(gain_db, transient=True)
                if muted is not None:
                    self.pipewire_mgr.set_channel_master_mute(ch, bool(muted))
                    if self._is_wave_channel(ch):
                        self.hardware_mgr.set_mode_mute("gain", bool(muted), transient=True)
                res["state"] = {
                    "volume": self.pipewire_mgr.get_channel_master_volume(ch),
                    "muted": self.pipewire_mgr.get_channel_master_mute(ch)
                }
            
            if self.pipewire_mgr.on_external_change_callback:
                from gi.repository import GLib
                GLib.idle_add(self.pipewire_mgr.on_external_change_callback, "channel", ch)
                
        elif cmd in ["get_mix_volume", "get_mix_master_volume"]:
            mx = self._match_mix_id(req.get("mix_id"))
            res["volume"] = self.pipewire_mgr.get_mix_master_volume(mx)
            res["muted"] = self.pipewire_mgr.get_mix_master_mute(mx)
        elif cmd in ["set_mix_volume", "set_mix_master_volume"]:
            mx = self._match_mix_id(req.get("mix_id"))
            vol = req.get("volume")
            muted = req.get("muted")
            if vol is not None:
                self.pipewire_mgr.set_mix_master_volume(mx, int(vol))
                if self._is_wave_mix(mx):
                    self.hardware_mgr.set_output_volume(volume_pct=int(vol), transient=True)
            if muted is not None:
                self.pipewire_mgr.set_mix_master_mute(mx, bool(muted))
                if self._is_wave_mix(mx):
                    self.hardware_mgr.set_mode_mute("hp", bool(muted), transient=True)
            res["volume"] = self.pipewire_mgr.get_mix_master_volume(mx)
            res["muted"] = self.pipewire_mgr.get_mix_master_mute(mx)
            if self.pipewire_mgr.on_external_change_callback:
                from gi.repository import GLib
                GLib.idle_add(self.pipewire_mgr.on_external_change_callback, "mix", mx)
        elif cmd in ["toggle_mix_mute", "toggle_mix_master_mute"]:
            mx = self._match_mix_id(req.get("mix_id"))
            is_muted = self.pipewire_mgr.toggle_mix_master_mute(mx)
            if self._is_wave_mix(mx):
                self.hardware_mgr.set_mode_mute("hp", is_muted, transient=True)
            res["muted"] = is_muted
            if self.pipewire_mgr.on_external_change_callback:
                from gi.repository import GLib
                GLib.idle_add(self.pipewire_mgr.on_external_change_callback, "mix", mx)
        elif cmd == "toggle_mute":
            raw_target = req.get("channel_id") or req.get("target") or "mic"
            ch = self._match_channel_id(raw_target)
            mx = self._match_mix_id(req.get("mix_id")) if req.get("mix_id") else None
            if mx:
                is_muted = self.pipewire_mgr.toggle_channel_mute(ch, mx)
            else:
                is_muted = self.pipewire_mgr.toggle_channel_master_mute(ch)
                if self._is_wave_channel(ch):
                    self.hardware_mgr.set_mode_mute("gain", is_muted, transient=True)
            if self.pipewire_mgr.on_external_change_callback:
                from gi.repository import GLib
                GLib.idle_add(self.pipewire_mgr.on_external_change_callback, "channel", ch)
            res["muted"] = is_muted
        elif cmd == "get_peaks":
            res["peaks"] = self.peak_monitor.get_all_peaks()
            # High-speed telemetry fusion: stream real-time mix & channel volumes at 30 FPS
            if hasattr(self, "pipewire_mgr") and self.pipewire_mgr:
                with self.pipewire_mgr._lock:
                    res["mix_states"] = dict(self.pipewire_mgr.mix_states)
                    res["channel_master_states"] = dict(getattr(self.pipewire_mgr, "channel_master_states", {}))
                    res["channel_states"] = {
                        k: dict(v) for k, v in self.pipewire_mgr.channel_states.items()
                    }
            if hasattr(self, "hardware_mgr") and self.hardware_mgr:
                res["hardware"] = {
                    "device_name": getattr(self.hardware_mgr, "device_name", ""),
                    "is_connected": self._is_hardware_connected(),
                    "gain_db": getattr(self.hardware_mgr, "hardware_gain_db", 0),
                    "phantom_48v": getattr(self.hardware_mgr, "phantom_power_48v", False),
                    "clipguard": getattr(self.hardware_mgr, "clipguard_enabled", False),
                    "elgato_info": self.hardware_mgr.get_elgato_device_info() if hasattr(self.hardware_mgr, "get_elgato_device_info") else {}
                }
        elif cmd == "get_hardware_status":
            res["device_name"] = self.hardware_mgr.device_name
            res["is_connected"] = self._is_hardware_connected()
            res["gain_db"] = self.hardware_mgr.hardware_gain_db
            res["phantom_48v"] = self.hardware_mgr.phantom_power_48v
            res["clipguard"] = self.hardware_mgr.clipguard_enabled
            res["low_cut"] = self.hardware_mgr.low_cut_filter
            res["low_impedance"] = self.hardware_mgr.low_impedance_mode
            res["monitor_mix_pct"] = self.hardware_mgr.get_monitor_mix()
            res["led_colors"] = getattr(self.hardware_mgr, "led_colors", {})
            res["elgato_info"] = self.hardware_mgr.get_elgato_device_info()
            from .elgato_wave import elgato_manager
            res["poll_thread_alive"] = bool(elgato_manager._poll_thread and elgato_manager._poll_thread.is_alive())
            res["on_state_changed_set"] = bool(elgato_manager.on_state_changed is not None)
            res["listeners_count"] = len(self.hardware_mgr._hardware_listeners)
            res["last_state"] = dict(elgato_manager.last_state) if hasattr(elgato_manager, "last_state") else {}
        elif cmd in ["set_hardware_gain", "set_gain"]:
            gain_val = req.get("gain_db")
            if gain_val is not None:
                self.hardware_mgr.set_gain(int(gain_val), transient=req.get("transient", False))
            elif "delta" in req:
                curr = self.hardware_mgr.hardware_gain_db
                self.hardware_mgr.set_gain(max(0, min(75, curr + int(req["delta"]))), transient=req.get("transient", False))
            res["gain_db"] = self.hardware_mgr.hardware_gain_db
        elif cmd == "set_monitor_mix":
            pct = req.get("percent", req.get("pct", 50))
            self.hardware_mgr.set_monitor_mix(int(pct), transient=req.get("transient", False))
            res["monitor_mix_pct"] = self.hardware_mgr.get_monitor_mix()
        elif cmd == "get_monitor_mix":
            res["monitor_mix_pct"] = self.hardware_mgr.get_monitor_mix()
        elif cmd == "set_led_color":
            mode = req.get("mode", "hp")
            color_hex = req.get("color", "#FFFFFF")
            self.hardware_mgr.set_led_color(mode, color_hex)
            res["led_colors"] = self.hardware_mgr.led_colors
        elif cmd == "toggle_phantom_power":
            res["phantom_48v"] = self.hardware_mgr.toggle_phantom_power()
        elif cmd == "toggle_clipguard":
            res["clipguard"] = self.hardware_mgr.toggle_clipguard()
        elif cmd == "set_low_cut":
            mode = req.get("mode", "80Hz")
            self.hardware_mgr.set_low_cut(mode)
            res["low_cut"] = self.hardware_mgr.low_cut_filter
        elif cmd == "toggle_low_impedance":
            res["low_impedance"] = self.hardware_mgr.toggle_low_impedance()
        elif cmd == "get_output_devices":
            devices = self.hardware_mgr.get_tracked_output_devices() if self.hardware_mgr else []
            res["devices"] = devices
        elif cmd == "set_mix_target_device":
            mx = self._match_mix_id(req.get("mix_id"))
            target_dev = req.get("target_device") or "none"
            self.pipewire_mgr.update_mix(mx, target_device=target_dev)
            res["target_device"] = target_dev
            if self.pipewire_mgr.on_external_change_callback:
                from gi.repository import GLib
                GLib.idle_add(self.pipewire_mgr.on_external_change_callback)
        elif cmd == "cycle_mix_target_device":
            mx = self._match_mix_id(req.get("mix_id"))
            devices = self.hardware_mgr.get_tracked_output_devices() if self.hardware_mgr else []
            dev_ids = [d["name"] for d in devices if "name" in d]
            if not dev_ids:
                dev_ids = ["none"]
            
            with self.pipewire_mgr._lock:
                mix_obj = next((m for m in self.pipewire_mgr.mixes if m["id"] == mx), None)
                curr_target = mix_obj.get("target_device", "none") if mix_obj else "none"
                try:
                    curr_idx = dev_ids.index(curr_target)
                    next_idx = (curr_idx + 1) % len(dev_ids)
                except ValueError:
                    next_idx = 0
                
                new_target = dev_ids[next_idx]
                self.pipewire_mgr.update_mix(mx, target_device=new_target)
                res["target_device"] = new_target
                if self.pipewire_mgr.on_external_change_callback:
                    from gi.repository import GLib
                    GLib.idle_add(self.pipewire_mgr.on_external_change_callback)
        else:
            res["status"] = "unknown_command"

        return res

    def _handle_client(self, conn):
        try:
            conn.settimeout(10.0)
            buffer = ""
            while self.running:
                try:
                    data = conn.recv(8192).decode('utf-8')
                except socket.timeout:
                    continue
                except Exception:
                    break

                if not data:
                    break
                
                buffer += data
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        req = json.loads(line)
                        res = self._process_command(req)
                        conn.sendall((json.dumps(res) + "\n").encode('utf-8'))
                    except Exception:
                        pass

                # Handle one-shot payloads without newline
                if buffer and "{" in buffer and "}" in buffer:
                    try:
                        req = json.loads(buffer.strip())
                        res = self._process_command(req)
                        conn.sendall((json.dumps(res) + "\n").encode('utf-8'))
                        buffer = ""
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
