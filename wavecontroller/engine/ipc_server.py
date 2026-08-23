import os
import json
import socket
import threading
import time

SOCKET_PATH = "/tmp/wavecontroller.sock"

class IPCServer:
    """
    Unix Domain Socket IPC Server allowing Volume Controller Plus and StreamController
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

    def stop(self):
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        if os.path.exists(SOCKET_PATH):
            try:
                os.remove(SOCKET_PATH)
            except Exception:
                pass

    def _run_server(self):
        if os.path.exists(SOCKET_PATH):
            try:
                os.remove(SOCKET_PATH)
            except Exception:
                pass

        try:
            self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server_sock.bind(SOCKET_PATH)
            self.server_sock.listen(5)
            self.server_sock.settimeout(1.0)
        except Exception:
            return

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

    def _handle_client(self, conn):
        try:
            conn.settimeout(2.0)
            data = conn.recv(4096).decode('utf-8')
            if not data:
                return
            
            req = json.loads(data)
            cmd = req.get("command")
            res = {"status": "ok"}

            if cmd == "get_channels":
                res["channels"] = self.pipewire_mgr.channels
                res["mixes"] = self.pipewire_mgr.mixes
                res["states"] = self.pipewire_mgr.channel_states
            elif cmd == "get_volume":
                raw_target = req.get("channel_id") or req.get("target") or "mic"
                ch = self._match_channel_id(raw_target)
                mx = req.get("mix_id")
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
                mx = req.get("mix_id")
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
                    if muted is not None:
                        self.pipewire_mgr.set_channel_master_mute(ch, bool(muted))
                    res["state"] = {
                        "volume": self.pipewire_mgr.get_channel_master_volume(ch),
                        "muted": self.pipewire_mgr.get_channel_master_mute(ch)
                    }
                
                if self.pipewire_mgr.on_external_change_callback:
                    from gi.repository import GLib
                    GLib.idle_add(self.pipewire_mgr.on_external_change_callback)
                    
            elif cmd == "toggle_mute":
                raw_target = req.get("channel_id") or req.get("target") or "mic"
                ch = self._match_channel_id(raw_target)
                mx = req.get("mix_id")
                if mx:
                    is_muted = self.pipewire_mgr.toggle_channel_mute(ch, mx)
                else:
                    is_muted = self.pipewire_mgr.toggle_channel_master_mute(ch)
                if self.pipewire_mgr.on_external_change_callback:
                    from gi.repository import GLib
                    GLib.idle_add(self.pipewire_mgr.on_external_change_callback)
                res["muted"] = is_muted
            elif cmd == "get_peaks":
                res["peaks"] = self.peak_monitor.get_all_peaks()
            elif cmd == "get_hardware_status":
                res["device_name"] = self.hardware_mgr.device_name
                res["gain_db"] = self.hardware_mgr.hardware_gain_db
                res["phantom_48v"] = self.hardware_mgr.phantom_power_48v
                res["clipguard"] = self.hardware_mgr.clipguard_enabled
                res["low_cut"] = self.hardware_mgr.low_cut_filter

            conn.sendall(json.dumps(res).encode('utf-8'))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
