"""
Submix & Sink Output Pipeline Manager
======================================
Dedicated manager for WaveController virtual null-sinks, matrix loopback faders,
and physical headphone DAC output routing.
Completely isolated from application discovery heuristics and microphone capture.
"""

import re
import json
import time
import subprocess
from wavecontroller.engine.graph.stream_resolver import resolve_physical_device_ports
from wavecontroller.engine.config_manager import config_manager
from wavecontroller.utils.logger import get_logger

log = get_logger("SubmixSinkManager")

class SubmixSinkManager:
    """
    Manages virtual mix sinks (Personal Mix, Stream Mix), submix loopback processes,
    and bridges to physical DAC / headphone hardware outputs.
    """
    def __init__(self, pipewire_mgr=None):
        self.pipewire_mgr = pipewire_mgr
        self._submix_procs = {}       # {(channel_id, mix_id): subprocess.Popen}
        self._submix_node_ids = {}    # {(channel_id, mix_id): [node_id, ...]}
        self._mix_node_ids_cache = {} # {mix_id: [node_id, ...]}

    def ensure_virtual_nodes(self, mixes: list, channels: list) -> bool:
        """
        Provisions necessary PipeWire null-audio-sink nodes for mixes and exposed group channels.
        Destroys stale/orphan nodes.
        """
        needed_nodes = {}
        for m in mixes:
            m_id = m["id"]
            m_name = m["name"]
            m_type = m.get("type", "source")

            if m_id == "personal" or m_type == "sink":
                node_name = f"WaveController_{m_id}_Sink"
                needed_nodes[node_name] = (f"WaveController {m_name} (Sink)", "Audio/Sink", False)
            else:
                node_name = f"WaveController_{m_id}_Source"
                needed_nodes[node_name] = (f"WaveController {m_name}", "Audio/Source", True)

        for ch in channels:
            ch_id = ch["id"]
            if ch.get("type") == "source":
                continue
            if ch.get("expose_sink", False):
                ch_name = ch.get("name", ch_id)
                node_name = f"WaveController_Channel_{ch_id}"
                needed_nodes[node_name] = (f"WaveController {ch_name} (Sink)", "Audio/Sink", False)

        existing_active_names = set()
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            valid_submix_names = {f"WaveController_submix_{ch['id']}_{mx['id']}" for ch in channels for mx in mixes}

            for obj in data:
                props = obj.get("info", {}).get("props", {})
                n_name = props.get("node.name", "")
                n_desc = props.get("node.description", "")
                if "WaveController_submix_" in n_name:
                    sub_clean = n_name.replace("input.", "").replace("output.", "")
                    if sub_clean not in valid_submix_names:
                        obj_id = obj.get("id")
                        if obj_id:
                            subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif n_name.startswith("WaveController_") or n_desc.startswith("WaveController "):
                    if n_name not in needed_nodes:
                        obj_id = obj.get("id")
                        if obj_id:
                            subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    elif n_name in existing_active_names:
                        # Duplicate node with identical name already tracked! Destroy duplicate to ensure strict 1:1 node cardinality
                        obj_id = obj.get("id")
                        if obj_id:
                            subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        existing_active_names.add(n_name)
        except Exception:
            pass

        nodes_created = False
        for node_name, node_tuple in needed_nodes.items():
            desc = node_tuple[0]
            media_class = node_tuple[1]
            is_source = node_tuple[2] if len(node_tuple) > 2 else False
            if node_name not in existing_active_names:
                try:
                    if is_source:
                        cmd = f'{{ factory.name=support.null-audio-sink node.name="{node_name}" node.description="{desc}" media.class={media_class} object.linger=true node.always-process=true node.passive=false }}'
                    else:
                        cmd = f'{{ factory.name=support.null-audio-sink node.name="{node_name}" node.description="{desc}" media.class={media_class} object.linger=true }}'
                    subprocess.run(["pw-cli", "create-node", "adapter", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    nodes_created = True
                except Exception:
                    pass

        if nodes_created:
            time.sleep(0.08)
        self._mix_node_ids_cache.clear()
        return nodes_created

    def sync_physical_output_routing(self, mixes: list, mix_id: str = None, out_ports: list = None, in_ports: list = None, 
                                     get_mix_mute_fn = None, links_map: dict = None):
        """
        Routes WaveController Sink mixes (Personal Mix, etc.) to physical output DAC / headphones.
        """
        if in_ports is None or out_ports is None or links_map is None:
            try:
                try:
                    o_raw = subprocess.check_output(["pw-link", "-I", "-o"], text=True, stderr=subprocess.DEVNULL)
                except Exception:
                    o_raw = ""
                if not o_raw:
                    o_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
                out_ports = [l.strip() for l in o_raw.splitlines() if l.strip()]
            except Exception:
                out_ports = []

            try:
                try:
                    i_raw = subprocess.check_output(["pw-link", "-I", "-i"], text=True, stderr=subprocess.DEVNULL)
                except Exception:
                    i_raw = ""
                if not i_raw:
                    i_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
                in_ports = [l.strip() for l in i_raw.splitlines() if l.strip()]
            except Exception:
                in_ports = []

        clean_out_ports = {re.sub(r"^\d+\s+", "", p).strip() for p in out_ports}
        clean_in_ports = [re.sub(r"^\d+\s+", "", p).strip() for p in in_ports]

        if links_map is None:
            links_map = {}
            try:
                out = subprocess.check_output(["pw-link", "-l"], text=True, stderr=subprocess.DEVNULL)
                cur_src = None
                for line in out.splitlines():
                    ls = line.strip()
                    if not line.startswith(" ") and ":" in ls:
                        cur_src = ls
                        links_map[cur_src] = set()
                    elif "|->" in ls and cur_src:
                        dest = ls.replace("|->", "").strip()
                        links_map[cur_src].add(dest)
            except Exception:
                pass

        mixes_to_sync = [m for m in mixes if mix_id is None or m["id"] == mix_id]

        for m in mixes_to_sync:
            m_id = m["id"]
            m_type = m.get("type", "source")

            is_personal = m_id in ("personal", "personal_mix") or (m_type == "sink" and "personal" in m_id)
            if is_personal:
                target_dev = config_manager.get("default_output_device", "") or m.get("target_device", "") or "default"
                if "wavecontroller" in str(target_dev).lower():
                    target_dev = "default"
                m["target_device"] = target_dev
            else:
                target_dev = m.get("target_device", "none" if not is_personal else "default")

            if m_type != "sink" and not is_personal:
                continue

            mon_fl = f"WaveController_{m_id}_Sink:monitor_FL"
            mon_fr = f"WaveController_{m_id}_Sink:monitor_FR"

            is_mix_muted = get_mix_mute_fn(m_id) if callable(get_mix_mute_fn) else False
            desired_fl = set()
            desired_fr = set()

            if target_dev and target_dev != "none" and not is_mix_muted:
                desired_fl, desired_fr = resolve_physical_device_ports(target_dev, clean_in_ports)
                
                # Resilient fallback for Personal Mix to ensure headphones are NEVER left unlinked
                if is_personal and (not desired_fl or not desired_fr):
                    alsa_playback_ports = [p for p in clean_in_ports if p.startswith("alsa_output.") and ":playback_" in p]
                    wave_ports = [p for p in alsa_playback_ports if "wave" in p.lower() or "elgato" in p.lower()]
                    candidate_ports = wave_ports or alsa_playback_ports
                    for p in candidate_ports:
                        suffix = p.split(":")[-1].lower()
                        if ("_fl" in suffix or suffix.endswith("_1") or suffix.endswith("_l") or suffix == "playback_0") and not desired_fl:
                            desired_fl.add(p)
                        elif ("_fr" in suffix or suffix.endswith("_2") or suffix.endswith("_r") or suffix == "playback_1") and not desired_fr:
                            desired_fr.add(p)

            # Reconcile FL links
            raw_fl_links = links_map.get(mon_fl, set())
            clean_fl_links = {re.sub(r"^\d+\s+", "", d).strip() for d in raw_fl_links if not d.isdigit()}
            for linked_dest in list(clean_fl_links):
                if linked_dest.startswith("alsa_output.") and linked_dest not in desired_fl:
                    try:
                        subprocess.run(["pw-link", "-d", mon_fl, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
            if mon_fl in clean_out_ports:
                for dest in desired_fl:
                    if dest not in clean_fl_links:
                        try:
                            subprocess.run(["pw-link", mon_fl, dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass

            # Reconcile FR links
            raw_fr_links = links_map.get(mon_fr, set())
            clean_fr_links = {re.sub(r"^\d+\s+", "", d).strip() for d in raw_fr_links if not d.isdigit()}
            for linked_dest in list(clean_fr_links):
                if linked_dest.startswith("alsa_output.") and linked_dest not in desired_fr:
                    try:
                        subprocess.run(["pw-link", mon_fr, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
            if mon_fr in clean_out_ports:
                for dest in desired_fr:
                    if dest not in clean_fr_links:
                        try:
                            subprocess.run(["pw-link", mon_fr, dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass
