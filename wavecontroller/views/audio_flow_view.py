"""
WaveController Audio Flow View
==============================
Interactive GTK4 + Libadwaita Audio Flow & PipeWire graph visualizer.
Featuring:
  1. Clean 3-Stage Live Audio Flow Pipeline:
     [CHANNELS]  -->  [MIXES]  -->  [OUTPUTS]
  2. Signal Path Focus Mode & Isolation:
     Clicking any node isolates its entire upstream and downstream routing path,
     brightly illuminating the active audio cables while smoothly dimming everything else.
  3. Fluid Animations:
     Smooth opacity easing transitions and animated signal flow pulses traveling
     along active audio cables.
  4. Cable Color Categories:
     - Teal / Cyan:    Input to Channel
     - Magenta / Pink: Channel to Mix
     - Spring Green:   Mix to Output
     - Orange:         Monitor / Loopback
     - Red:            Muted Cable
     - Translucent:    Not in path
  5. Draggable & Persistent Node Positions:
     Cards can be freely moved and positions are automatically saved to
     ~/.config/WaveController/audio_flow_positions.json.
"""

import os
import json
import math
import cairo

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk

from .fx_sidebar import FXSidebar
from ..utils.logger import get_logger

log = get_logger("AudioFlowView")

POSITIONS_FILE = os.path.join(os.path.expanduser("~/.config/WaveController"), "audio_flow_positions.json")

# ── Colour Palette ──────────────────────────────────────────────────────────
# Canvas & Cards
CLR_CANVAS       = (0.09, 0.09, 0.11)       # deep dark canvas
CLR_GRID_DOT     = (1.0,  1.0,  1.0, 0.04)  # very subtle grid
CLR_CARD_BG      = (0.13, 0.14, 0.17)       # dark card body
CLR_CARD_BDR     = (0.22, 0.24, 0.29)       # default card border
CLR_CARD_SELECT  = (0.24, 0.58, 1.00)       # vibrant blue selection border
CLR_CARD_PATH    = (0.42, 0.45, 0.56)       # path-highlighted border

# Cable categories
CLR_CABLE_IN_CH  = (0.18, 0.80, 0.78)       # teal / cyan (input -> channel)
CLR_CABLE_CH_MIX = (0.86, 0.34, 0.84)       # magenta / purple (channel -> mix)
CLR_CABLE_MIX_OUT= (0.24, 0.88, 0.52)       # spring green (mix -> output)
CLR_CABLE_MONITOR= (0.95, 0.62, 0.16)       # orange monitor link
CLR_CABLE_MUTED  = (0.92, 0.28, 0.28)       # red muted cable
CLR_CABLE_DIMMED = (0.35, 0.38, 0.45)       # dim gray (not in path)

# Text & Ports
CLR_TEXT_TITLE   = (0.94, 0.95, 0.98)
CLR_TEXT_SUB     = (0.58, 0.62, 0.70)
CLR_PORT_BORDER  = (1.0,  1.0,  1.0, 0.40)

# Dimensions
CARD_WIDTH        = 240
CARD_HEIGHT       = 58
CARD_BORDER_R     = 10
PORT_RADIUS       = 6
PORT_HIT_RADIUS   = 14
GRID_SPACING      = 28
GRID_DOT_SIZE     = 1.2
BEND_POINT_RADIUS = 7
BEND_POINT_HIT    = 12
DEFAULT_CABLE_GAP = 18
ROUTE_FORMAT_VERSION = 3
ZOOM_MIN          = 0.25
ZOOM_MAX          = 3.5
ZOOM_STEP         = 0.10

# Styles
LINE_STYLE_BEZIER   = "bezier"
LINE_STYLE_STRAIGHT = "straight"

# Stage columns configuration (Clean 3-Stage Pipeline: CHANNELS -> MIXES -> OUTPUTS)
STAGES = [
    {"id": "channel", "title": "CHANNELS", "icon": "audio-input-microphone-symbolic", "x": 100,  "desc": "Mixer Channel Strips"},
    {"id": "mix",     "title": "MIXES",    "icon": "audio-speakers-symbolic",         "x": 580,  "desc": "Virtual Submix Buses"},
    {"id": "output",  "title": "OUTPUTS",  "icon": "audio-headset-symbolic",          "x": 1060, "desc": "Physical & Virtual Outputs"},
]


# ═══════════════════════════════════════════════════════════════════════════
#  Data Models
# ═══════════════════════════════════════════════════════════════════════════

class Port:
    """Represents a single input or output port on a node card."""
    __slots__ = ("name", "direction", "index", "node")

    def __init__(self, name: str, direction: str, index: int, node: "GraphNode"):
        self.name = name
        self.direction = direction  # "in" or "out"
        self.index = index
        self.node = node

    def get_canvas_pos(self) -> tuple:
        """Returns (cx, cy) of the port circle in canvas coordinates."""
        n = self.node
        port_list = n.inputs if self.direction == "in" else n.outputs
        total = len(port_list)
        if total <= 1:
            py = n.y + n.height / 2.0
        else:
            spacing = min(14.0, (n.height - 18.0) / (total - 1))
            start_y = n.y + (n.height - spacing * (total - 1)) / 2.0
            py = start_y + self.index * spacing

        if self.direction == "out":
            px = n.x + n.width
        else:
            px = n.x
        return px, py


class GraphNode:
    """A card on the Audio Flow canvas."""

    def __init__(self, node_id: str, label: str, node_type: str,
                 inputs: list, outputs: list, x: float = 0, y: float = 0,
                 stage: str = "channel", subtitle: str = "", icon: str = "generic",
                 muted: bool = False):
        self.node_id = node_id
        self.label = label
        self.node_type = node_type  # "source", "sink", "filter"
        self.stage = stage          # "input", "channel", "mix", "output"
        self.subtitle = subtitle
        self.icon = icon
        self.muted = muted
        self.x = x
        self.y = y
        self.locked = False
        self.selected = False
        self.dimmed = False         # search filter dimming
        self.highlighted = False    # signal path isolation highlight
        self.current_alpha = 1.0    # animated alpha
        self.target_alpha = 1.0     # target alpha

        self.inputs = [Port(name, "in", i, self) for i, name in enumerate(inputs)]
        self.outputs = [Port(name, "out", i, self) for i, name in enumerate(outputs)]

        self.width = CARD_WIDTH
        self.height = CARD_HEIGHT

    def contains(self, cx: float, cy: float) -> bool:
        return (self.x <= cx <= self.x + self.width and
                self.y <= cy <= self.y + self.height)

    def port_at(self, cx: float, cy: float):
        for p in self.outputs + self.inputs:
            px, py = p.get_canvas_pos()
            if math.hypot(cx - px, cy - py) <= PORT_HIT_RADIUS:
                return p
        return None


class Connection:
    """A link between an output port and an input port with category and animation."""
    __slots__ = (
        "src_port", "dst_port", "conn_type", "category", "hovered",
        "bend_points", "user_routed", "src_offset", "dst_offset", "lane_offset",
        "highlighted", "current_alpha", "target_alpha", "muted"
    )

    def __init__(self, src_port: Port, dst_port: Port, conn_type: str = "audio", category: str = "ch_to_mix"):
        self.src_port = src_port
        self.dst_port = dst_port
        self.conn_type = conn_type
        self.category = category  # "in_to_ch", "ch_to_mix", "mix_to_out", "monitor"
        self.hovered = False
        self.bend_points = []
        self.user_routed = False
        self.src_offset = 0.0
        self.dst_offset = 0.0
        self.lane_offset = 0.0
        self.highlighted = False
        self.current_alpha = 1.0
        self.target_alpha = 1.0
        self.muted = False

    @property
    def color_rgb(self):
        if self.muted:
            return CLR_CABLE_MUTED
        if self.category == "in_to_ch":
            return CLR_CABLE_IN_CH
        elif self.category == "ch_to_mix":
            return CLR_CABLE_CH_MIX
        elif self.category == "mix_to_out":
            return CLR_CABLE_MIX_OUT
        elif self.category == "monitor" or self.conn_type == "monitor":
            return CLR_CABLE_MONITOR
        return CLR_CABLE_CH_MIX

    def get_segments(self, orthogonal: bool = False, nodes=None) -> list:
        src = list(self.src_port.get_canvas_pos())
        dst = list(self.dst_port.get_canvas_pos())

        src[1] += self.src_offset
        dst[1] += self.dst_offset

        stub_len = 24.0
        routed_src = [src[0] + stub_len, src[1]]
        routed_dst = [dst[0] - stub_len, dst[1]]

        if not orthogonal:
            pts = [src]
            pts.extend(self.bend_points)
            pts.append(dst)
            return pts

        if self.user_routed and self.bend_points:
            points = [src, routed_src]
            curr = routed_src
            for bp in self.bend_points:
                points.append([bp[0], curr[1]])
                points.append(bp)
                curr = bp
            points.append([curr[0], routed_dst[1]])
            points.append(routed_dst)
            points.append(dst)
            return points

        x_mid = (src[0] + dst[0]) / 2.0 + self.lane_offset
        return [
            src,
            routed_src,
            [x_mid, routed_src[1]],
            [x_mid, routed_dst[1]],
            routed_dst,
            dst
        ]


# ═══════════════════════════════════════════════════════════════════════════
#  Position Persistence
# ═══════════════════════════════════════════════════════════════════════════

def save_positions(nodes: dict, connections=None, line_style=None, cable_gap=None):
    os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
    data = {}
    data["_route_format"] = ROUTE_FORMAT_VERSION
    for nid, n in nodes.items():
        data[nid] = {"x": n.x, "y": n.y, "locked": n.locked}
    if connections is not None:
        data["_connections"] = [
            {
                "src": [c.src_port.node.node_id, c.src_port.name],
                "dst": [c.dst_port.node.node_id, c.dst_port.name],
                "bends": c.bend_points,
                "user_routed": c.user_routed,
            }
            for c in connections
            if c.user_routed and c.bend_points
        ]
    if line_style in (LINE_STYLE_BEZIER, LINE_STYLE_STRAIGHT):
        data["_line_style"] = line_style
    if cable_gap is not None:
        data["_cable_gap"] = cable_gap
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save positions to {POSITIONS_FILE}: {e}")


def load_positions(nodes: dict, connections=None, return_settings=False):
    target_file = POSITIONS_FILE

    if not os.path.exists(target_file):
        return {} if return_settings else None
    try:
        with open(target_file, "r") as f:
            data = json.load(f)

        format_ver = data.get("_route_format", 1)
        if format_ver < ROUTE_FORMAT_VERSION:
            line_style = data.get("_line_style")
            if line_style not in (LINE_STYLE_BEZIER, LINE_STYLE_STRAIGHT):
                line_style = None
            if return_settings:
                return {
                    "line_style": line_style,
                    "cable_gap": data.get("_cable_gap"),
                }
            return line_style

        for nid, pos in data.items():
            if nid.startswith("_"):
                continue
            if nid in nodes:
                nodes[nid].x = pos.get("x", nodes[nid].x)
                nodes[nid].y = pos.get("y", nodes[nid].y)
                nodes[nid].locked = pos.get("locked", False)
        line_style = data.get("_line_style")
        if line_style not in (LINE_STYLE_BEZIER, LINE_STYLE_STRAIGHT):
            line_style = None
        if return_settings:
            return {
                "line_style": line_style,
                "cable_gap": data.get("_cable_gap"),
            }
        return line_style
    except Exception as e:
        log.warning(f"Could not load positions from {target_file}: {e}")
        return {} if return_settings else None


# ═══════════════════════════════════════════════════════════════════════════
#  Topology Builders (Live PipeWire & Demo Fallback)
# ═══════════════════════════════════════════════════════════════════════════

def build_dummy_graph():
    """Fallback 3-stage Audio Flow demo topology."""
    nodes = {}

    # Stage 0: CHANNELS
    nodes["ch_mic"] = GraphNode(
        "ch_mic", "Microphone", "source",
        inputs=[], outputs=["monitor_FL", "monitor_FR"],
        x=100, y=110, stage="channel", subtitle="Hardware Mic · Vol 100%", icon="mic"
    )
    nodes["ch_desktop"] = GraphNode(
        "ch_desktop", "Desktop Audio", "source",
        inputs=[], outputs=["monitor_FL", "monitor_FR"],
        x=100, y=190, stage="channel", subtitle="System Audio · Vol 100%", icon="generic"
    )
    nodes["ch_spotify"] = GraphNode(
        "ch_spotify", "Spotify", "source",
        inputs=[], outputs=["monitor_FL", "monitor_FR"],
        x=100, y=270, stage="channel", subtitle="Music · Vol 75%", icon="spotify"
    )
    nodes["ch_discord"] = GraphNode(
        "ch_discord", "Discord", "source",
        inputs=[], outputs=["monitor_FL", "monitor_FR"],
        x=100, y=350, stage="channel", subtitle="Voice Chat · Vol 90%", icon="discord"
    )

    # Stage 1: MIXES
    nodes["mix_personal"] = GraphNode(
        "mix_personal", "Personal Mix", "filter",
        inputs=["playback_FL", "playback_FR"], outputs=["monitor_FL", "monitor_FR"],
        x=580, y=140, stage="mix", subtitle="Monitor Mix · Vol 85%", icon="headphones"
    )
    nodes["mix_stream"] = GraphNode(
        "mix_stream", "Stream Mix", "filter",
        inputs=["playback_FL", "playback_FR"], outputs=["monitor_FL", "monitor_FR"],
        x=580, y=260, stage="mix", subtitle="Virtual Input · Vol 100%", icon="broadcast"
    )

    # Stage 2: OUTPUTS
    nodes["out_headphones"] = GraphNode(
        "out_headphones", "Headphones (Elgato Wave XLR)", "sink",
        inputs=["playback_FL", "playback_FR"], outputs=[],
        x=1060, y=140, stage="output", subtitle="Hardware Output", icon="headphones"
    )
    nodes["out_stream_target"] = GraphNode(
        "out_stream_target", "Virtual Input", "sink",
        inputs=["playback_FL", "playback_FR"], outputs=[],
        x=1060, y=260, stage="output", subtitle="Stream & Apps Target", icon="broadcast"
    )

    connections = []

    def connect(src_id, src_p, dst_id, dst_p, cat="ch_to_mix", ctype="audio"):
        if src_id in nodes and dst_id in nodes:
            sp = next((p for p in nodes[src_id].outputs if p.name == src_p), None)
            dp = next((p for p in nodes[dst_id].inputs if p.name == dst_p), None)
            if sp and dp:
                connections.append(Connection(sp, dp, conn_type=ctype, category=cat))

    # Channels -> Mixes
    connect("ch_mic", "monitor_FL", "mix_stream", "playback_FL")
    connect("ch_mic", "monitor_FR", "mix_stream", "playback_FR")

    connect("ch_desktop", "monitor_FL", "mix_personal", "playback_FL")
    connect("ch_desktop", "monitor_FR", "mix_personal", "playback_FR")
    connect("ch_desktop", "monitor_FL", "mix_stream", "playback_FL")
    connect("ch_desktop", "monitor_FR", "mix_stream", "playback_FR")

    connect("ch_spotify", "monitor_FL", "mix_personal", "playback_FL")
    connect("ch_spotify", "monitor_FR", "mix_personal", "playback_FR")

    connect("ch_discord", "monitor_FL", "mix_personal", "playback_FL")
    connect("ch_discord", "monitor_FR", "mix_personal", "playback_FR")

    # Mixes -> Outputs
    connect("mix_personal", "monitor_FL", "out_headphones", "playback_FL", cat="mix_to_out")
    connect("mix_personal", "monitor_FR", "out_headphones", "playback_FR", cat="mix_to_out")

    connect("mix_stream", "monitor_FL", "out_stream_target", "playback_FL", cat="mix_to_out")
    connect("mix_stream", "monitor_FR", "out_stream_target", "playback_FR", cat="mix_to_out")

    return nodes, connections


def build_live_graph(pipewire_mgr, hardware_mgr):
    """
    Dynamically builds Audio Flow nodes and cables reflecting live WaveController state:
    [CHANNELS] --> [MIXES] --> [OUTPUTS]

    Strictly mirrors configured channels and submixes.
    Unassigned background applications or dangling system streams are excluded.
    """
    if not pipewire_mgr or not getattr(pipewire_mgr, "channels", None):
        return build_dummy_graph()

    nodes = {}
    connections = []

    def connect(src_id, src_p, dst_id, dst_p, cat="ch_to_mix", ctype="audio", muted=False):
        if src_id not in nodes or dst_id not in nodes:
            return
        sn, dn = nodes[src_id], nodes[dst_id]
        sp = next((p for p in sn.outputs if p.name == src_p), None)
        dp = next((p for p in dn.inputs if p.name == dst_p), None)
        if sp and dp:
            conn = Connection(sp, dp, conn_type=ctype, category=cat)
            conn.muted = muted
            connections.append(conn)

    start_y_ch = 110
    spacing_y_ch = 80

    # ── Stage 0: CHANNELS (Strictly Active WaveController Channels) ──
    for i, ch in enumerate(pipewire_mgr.channels):
        ch_id = ch["id"]
        node_id = f"ch_{ch_id}"
        m_st = pipewire_mgr.channel_master_states.get(ch_id, {})
        vol = m_st.get("volume", 80)
        muted = m_st.get("muted", False)

        assigned = pipewire_mgr.assigned_apps.get(ch_id, [])
        assigned_str = assigned[0] if assigned else ""

        ch_name = ch.get("name", ch_id)
        if muted:
            sub = f"Vol {vol}% · Muted"
        elif assigned_str and assigned_str.lower() != ch_name.lower():
            sub = f"{assigned_str} · Vol {vol}%"
        elif ch.get("type") in ("source", "mic") or "mic" in ch_id.lower():
            sub = f"Hardware Mic · Vol {vol}%"
        elif "desktop" in ch_id.lower() or "system" in ch_id.lower():
            sub = f"System Audio · Vol {vol}%"
        else:
            sub = f"Vol {vol}%"

        ch_name_low = ch_name.lower()
        ch_id_low = ch_id.lower()
        icon = "generic"
        if "mic" in ch_name_low or "mic" in ch_id_low or ch.get("type") in ("source", "mic"):
            icon = "mic"
        elif "chrome" in ch_name_low or "browser" in ch_name_low:
            icon = "chrome"
        elif "discord" in ch_name_low or "chat" in ch_name_low or "voice" in ch_name_low:
            icon = "discord"
        elif "spotify" in ch_name_low or "music" in ch_name_low:
            icon = "spotify"
        elif "deck" in ch_name_low:
            icon = "stream_deck"
        elif "game" in ch_name_low or "cam" in ch_name_low:
            icon = "cam"
        elif "aux" in ch_name_low or "line" in ch_name_low:
            icon = "aux"

        nodes[node_id] = GraphNode(
            node_id, ch_name, "source",
            inputs=[], outputs=["monitor_FL", "monitor_FR"],
            x=100, y=start_y_ch + i * spacing_y_ch,
            stage="channel", subtitle=sub, icon=icon, muted=muted
        )

    # ── Stage 1: MIXES (Strictly Configured Virtual Submixes) ──
    start_y_mix = 110
    spacing_y_mix = 110
    for i, mix in enumerate(pipewire_mgr.mixes):
        m_id = mix["id"]
        node_id = f"mix_{m_id}"
        m_st = pipewire_mgr.mix_states.get(m_id, {})
        vol = m_st.get("volume", 100)
        muted = m_st.get("muted", False)

        m_type = mix.get("type", "sink")
        target_dev = (mix.get("target_device") or "").strip()
        if muted:
            sub = f"Vol {vol}% · Muted"
        elif m_type == "source" or target_dev in ("none", "", None):
            sub = f"Virtual Input · Vol {vol}%"
        else:
            sub = f"Monitor Mix · Vol {vol}%"

        m_name_low = mix.get("name", "").lower()
        icon = "headphones"
        if "stream" in m_name_low:
            icon = "broadcast"
        elif "chat" in m_name_low:
            icon = "chat"
        elif "vod" in m_name_low or "record" in m_name_low:
            icon = "tape"

        nodes[node_id] = GraphNode(
            node_id, mix.get("name", m_id), "filter",
            inputs=["playback_FL", "playback_FR"], outputs=["monitor_FL", "monitor_FR"],
            x=580, y=start_y_mix + i * spacing_y_mix,
            stage="mix", subtitle=sub, icon=icon, muted=muted
        )

    # ── Stage 2: OUTPUTS (Hardware Listening Devices & Virtual Stream Sinks) ──
    start_y_out = 110
    spacing_y_out = 110
    output_items = []
    seen_outputs = set()

    # Tracked physical hardware output devices
    if hardware_mgr:
        tracked = hardware_mgr.get_tracked_devices()
        for dev in tracked:
            if dev.get("type") in ("output", "duplex"):
                k = dev.get("device_key", dev.get("name", "dev"))
                clean_id = f"out_{k}".replace(" ", "_").replace("-", "_").replace(".", "_")
                if clean_id not in seen_outputs:
                    seen_outputs.add(clean_id)
                    icon = "headphones" if "headphone" in dev.get("name", "").lower() or dev.get("is_elgato") else "speaker"
                    output_items.append({
                        "id": clean_id,
                        "name": dev.get("display_name", dev.get("name", "Output Device")),
                        "subtitle": "Hardware Output",
                        "icon": icon,
                        "is_virtual": False,
                        "match_keys": [dev.get("name", ""), dev.get("display_name", ""), k]
                    })

    # Virtual Input target (for virtual source mixes like Chat Mix or Stream Mix)
    has_virtual_mix = any(m.get("type") == "source" or (m.get("target_device") or "").strip() in ("none", "", None) for m in pipewire_mgr.mixes)
    if has_virtual_mix:
        virt_id = "out_virtual_stream_target"
        seen_outputs.add(virt_id)
        output_items.append({
            "id": virt_id,
            "name": "Virtual Input",
            "subtitle": "Apps & Stream Target",
            "icon": "chat",
            "is_virtual": True,
            "match_keys": ["virtual", "source", "none"]
        })

    if not output_items:
        output_items.append({
            "id": "out_default",
            "name": "System Audio Output",
            "subtitle": "Default Sound Card",
            "icon": "headphones",
            "is_virtual": False,
            "match_keys": ["default", "system"]
        })

    for i, out_dev in enumerate(output_items):
        nodes[out_dev["id"]] = GraphNode(
            out_dev["id"], out_dev["name"], "sink",
            inputs=["playback_FL", "playback_FR"], outputs=[],
            x=1060, y=start_y_out + i * spacing_y_out,
            stage="output", subtitle=out_dev["subtitle"], icon=out_dev["icon"]
        )

    # ── Link Channels -> Mixes ──
    for ch in pipewire_mgr.channels:
        ch_id = ch["id"]
        ch_node_id = f"ch_{ch_id}"
        ch_st = pipewire_mgr.channel_states.get(ch_id, {})
        master_st = pipewire_mgr.channel_master_states.get(ch_id, {})
        master_muted = master_st.get("muted", False)

        for mix in pipewire_mgr.mixes:
            m_id = mix["id"]
            mix_node_id = f"mix_{m_id}"

            is_enabled = pipewire_mgr.is_channel_mix_enabled(ch_id, m_id)
            if is_enabled:
                st = ch_st.get(m_id, {})
                sub_muted = st.get("muted", False) or master_muted
                connect(ch_node_id, "monitor_FL", mix_node_id, "playback_FL", cat="ch_to_mix", muted=sub_muted)
                connect(ch_node_id, "monitor_FR", mix_node_id, "playback_FR", cat="ch_to_mix", muted=sub_muted)

    # ── Link Mixes -> Outputs ──
    for mix in pipewire_mgr.mixes:
        m_id = mix["id"]
        mix_node_id = f"mix_{m_id}"
        m_st = pipewire_mgr.mix_states.get(m_id, {})
        mix_muted = m_st.get("muted", False)
        target_dev = (mix.get("target_device") or "").strip()
        target_dev_low = target_dev.lower()
        is_source_mix = mix.get("type") == "source" or target_dev in ("none", "", None)

        matched_out = None
        if is_source_mix and "out_virtual_stream_target" in nodes:
            matched_out = "out_virtual_stream_target"
        elif target_dev:
            for out in output_items:
                if out.get("is_virtual"):
                    continue
                for mk in out["match_keys"]:
                    if mk.lower() in target_dev_low or target_dev_low in mk.lower():
                        matched_out = out["id"]
                        break
                if matched_out:
                    break

        if not matched_out:
            hw_outs = [o for o in output_items if not o.get("is_virtual")]
            if hw_outs:
                matched_out = hw_outs[0]["id"]
            elif output_items:
                matched_out = output_items[0]["id"]

        if matched_out:
            connect(mix_node_id, "monitor_FL", matched_out, "playback_FL", cat="mix_to_out", muted=mix_muted)
            connect(mix_node_id, "monitor_FR", matched_out, "playback_FR", cat="mix_to_out", muted=mix_muted)

    return nodes, connections


# ═══════════════════════════════════════════════════════════════════════════
#  Canvas Widget — Cairo Drawing & Fluid Interactivity
# ═══════════════════════════════════════════════════════════════════════════

class AudioFlowCanvas(Gtk.DrawingArea):
    """High-performance Cairo drawing area rendering Audio Flow topology."""

    def __init__(self, view_container):
        super().__init__()
        self.view = view_container
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_focus(True)
        self.set_focusable(True)

        self.nodes = {}
        self.connections = []
        self.line_style = LINE_STYLE_BEZIER
        self.cable_gap = DEFAULT_CABLE_GAP

        # View transform
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0

        # Interaction & Focus Mode
        self.selected_node = None
        self._drag_node = None
        self._drag_offset = (0, 0)
        self._panning = False
        self._pan_start = (0, 0)
        self._pan_start_offset = (0, 0)

        # Connection creation / drag preview
        self._connecting_from = None
        self._connect_preview = None
        self._connecting_active = False

        # Bend point dragging
        self._drag_bend_conn = None
        self._drag_bend_idx = -1
        self._drag_bend_offset = (0, 0)

        # Hover & Context
        self._hover_port = None
        self._hover_node = None
        self._selected_connection = None
        self.global_lock = False

        # Animation state (60 FPS tick)
        self._anim_phase = 0.0
        self._save_timer_id = None

        self.set_draw_func(self._draw)
        self.add_tick_callback(self._on_tick)

        # Event controllers
        click_ctrl = Gtk.GestureClick.new()
        click_ctrl.set_button(0)
        click_ctrl.connect("pressed", self._on_pressed)
        click_ctrl.connect("released", self._on_released)
        self.add_controller(click_ctrl)

        motion_ctrl = Gtk.EventControllerMotion.new()
        motion_ctrl.connect("motion", self._on_motion)
        self.add_controller(motion_ctrl)

        drag_ctrl = Gtk.GestureDrag.new()
        drag_ctrl.set_button(1)
        drag_ctrl.connect("drag-begin", self._on_drag_begin)
        drag_ctrl.connect("drag-update", self._on_drag_update)
        drag_ctrl.connect("drag-end", self._on_drag_end)
        self.add_controller(drag_ctrl)

        scroll_ctrl = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_ctrl.connect("scroll", self._on_scroll)
        self.add_controller(scroll_ctrl)

        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

    def load_topology(self, pipewire_mgr, hardware_mgr):
        """Builds and loads the topology with saved position caching."""
        self.nodes, self.connections = build_live_graph(pipewire_mgr, hardware_mgr)
        saved_settings = load_positions(self.nodes, self.connections, return_settings=True)
        if saved_settings.get("line_style"):
            self.line_style = saved_settings["line_style"]
        if saved_settings.get("cable_gap"):
            self.cable_gap = max(4, float(saved_settings["cable_gap"]))

        self.update_port_fanout()
        self.queue_draw()

    # ── Animation Loop ──────────────────────────────────────────────────

    def _on_tick(self, widget, frame_clock):
        # Visibility Guard: Drop background render cycles when hidden / minimized / in other tabs
        if not self.get_mapped():
            return True

        # 1. Advance flowing pulse phase
        self._anim_phase = (self._anim_phase + 0.016) % 1.0

        # 2. Smoothly ease node alphas
        for n in self.nodes.values():
            diff = n.target_alpha - n.current_alpha
            if abs(diff) > 0.005:
                n.current_alpha += diff * 0.22
            else:
                n.current_alpha = n.target_alpha

        # 3. Smoothly ease connection alphas
        for c in self.connections:
            diff = c.target_alpha - c.current_alpha
            if abs(diff) > 0.005:
                c.current_alpha += diff * 0.22
            else:
                c.current_alpha = c.target_alpha

        self.queue_draw()
        return True

    # ── Focus & Signal Path Isolation ───────────────────────────────────

    def select_node(self, node: GraphNode):
        """Isolates the signal path for the selected node or clears isolation."""
        self.selected_node = node
        for n in self.nodes.values():
            n.selected = (n is node)

        self.update_signal_path_isolation(node)

        if hasattr(self.view, "btn_clear_sel"):
            self.view.btn_clear_sel.set_visible(node is not None or getattr(self, "_selected_connection", None) is not None)

        if hasattr(self.view, "update_signal_strip"):
            self.view.update_signal_strip(node=node, conn=None)

        if node:
            in_nodes = sum(1 for n in self.nodes.values() if n.highlighted)
            in_conns = sum(1 for c in self.connections if c.highlighted)
            self._update_status(f"Signal Path: {node.label} ({in_nodes} cards, {in_conns} links in path)")
        else:
            self._update_status("Ready — Click any node to isolate its signal flow path")

        self.queue_draw()

    def update_signal_path_isolation(self, selected_node: GraphNode = None):
        """Calculates upstream and downstream dependency graph for the node."""
        if selected_node is None:
            for n in self.nodes.values():
                n.highlighted = False
                n.target_alpha = 1.0
            for c in self.connections:
                c.highlighted = False
                c.target_alpha = 1.0
            return

        # Upstream traversal
        upstream_nodes = set()
        upstream_conns = set()
        queue = [selected_node]
        while queue:
            curr = queue.pop(0)
            for c in self.connections:
                if c.dst_port.node == curr:
                    upstream_conns.add(c)
                    if c.src_port.node not in upstream_nodes:
                        upstream_nodes.add(c.src_port.node)
                        queue.append(c.src_port.node)

        # Downstream traversal
        downstream_nodes = set()
        downstream_conns = set()
        queue = [selected_node]
        while queue:
            curr = queue.pop(0)
            for c in self.connections:
                if c.src_port.node == curr:
                    downstream_conns.add(c)
                    if c.dst_port.node not in downstream_nodes:
                        downstream_nodes.add(c.dst_port.node)
                        queue.append(c.dst_port.node)

        active_nodes = {selected_node} | upstream_nodes | downstream_nodes
        active_conns = upstream_conns | downstream_conns

        for n in self.nodes.values():
            if n in active_nodes:
                n.highlighted = True
                n.target_alpha = 1.0
            else:
                n.highlighted = False
                n.target_alpha = 0.14

        for c in self.connections:
            if c in active_conns:
                c.highlighted = True
                c.target_alpha = 1.0
            else:
                c.highlighted = False
                c.target_alpha = 0.08

    # ── Coordinates & Hit Testing ───────────────────────────────────────

    def screen_to_canvas(self, sx, sy):
        cx = (sx - self.pan_x) / self.zoom
        cy = (sy - self.pan_y) / self.zoom
        return cx, cy

    def canvas_to_screen(self, cx, cy):
        sx = cx * self.zoom + self.pan_x
        sy = cy * self.zoom + self.pan_y
        return sx, sy

    def node_at(self, cx, cy):
        for n in reversed(list(self.nodes.values())):
            if n.contains(cx, cy):
                return n
        return None

    def port_at(self, cx, cy):
        for n in self.nodes.values():
            p = n.port_at(cx, cy)
            if p:
                return p
        return None

    def connection_at(self, cx, cy, threshold=8.0):
        best_conn = None
        best_dist = threshold
        for conn in self.connections:
            d = self._distance_to_connection(conn, cx, cy)
            if d < best_dist:
                best_dist = d
                best_conn = conn
        return best_conn

    def _distance_to_connection(self, conn, cx, cy):
        pts = conn.get_segments(orthogonal=(self.line_style == LINE_STYLE_STRAIGHT), nodes=self.nodes)
        min_d = float("inf")
        for i in range(len(pts) - 1):
            p1, p2 = pts[i], pts[i + 1]
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            l2 = dx*dx + dy*dy
            if l2 == 0:
                d = math.hypot(cx - p1[0], cy - p1[1])
            else:
                t = max(0.0, min(1.0, ((cx - p1[0]) * dx + (cy - p1[1]) * dy) / l2))
                proj_x = p1[0] + t * dx
                proj_y = p1[1] + t * dy
                d = math.hypot(cx - proj_x, cy - proj_y)
            if d < min_d:
                min_d = d
        return min_d

    def update_port_fanout(self):
        src_groups, dst_groups = {}, {}
        for conn in self.connections:
            src_groups.setdefault(conn.src_port, []).append(conn)
            dst_groups.setdefault(conn.dst_port, []).append(conn)

        stub_spacing = min(10.0, self.cable_gap * 0.30)
        for port, conns in src_groups.items():
            center = (len(conns) - 1) / 2.0
            for idx, c in enumerate(conns):
                c.src_offset = (idx - center) * stub_spacing

        for port, conns in dst_groups.items():
            center = (len(conns) - 1) / 2.0
            for idx, c in enumerate(conns):
                c.dst_offset = (idx - center) * stub_spacing

    # ── Drawing ─────────────────────────────────────────────────────────

    def _draw(self, area, cr, width, height):
        # Canvas background
        cr.set_source_rgb(*CLR_CANVAS)
        cr.paint()

        # Grid dots
        cr.save()
        cr.set_source_rgba(*CLR_GRID_DOT)
        spacing = GRID_SPACING * self.zoom
        if spacing > 5:
            start_x = -self.pan_x % spacing
            start_y = -self.pan_y % spacing
            x = start_x
            while x < width:
                y = start_y
                while y < height:
                    cr.rectangle(x - GRID_DOT_SIZE/2, y - GRID_DOT_SIZE/2, GRID_DOT_SIZE, GRID_DOT_SIZE)
                    y += spacing
                x += spacing
            cr.fill()
        cr.restore()

        # Canvas transform
        cr.save()
        cr.translate(self.pan_x, self.pan_y)
        cr.scale(self.zoom, self.zoom)

        # Stage Column Headers
        self._draw_stage_headers(cr)

        # Cables
        for conn in self.connections:
            self._draw_connection(cr, conn)

        # Connection drag preview
        if self._connecting_from and self._connect_preview:
            self._draw_connection_preview(cr)

        # Cards
        for n in self.nodes.values():
            self._draw_card(cr, n)

        cr.restore()

    def _draw_stage_headers(self, cr):
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)

        stage_colors = {
            "input":   (0.18, 0.80, 0.78),
            "channel": (0.35, 0.65, 0.98),
            "mix":     (0.86, 0.34, 0.84),
            "output":  (0.24, 0.88, 0.52),
        }

        for stage in STAGES:
            sx = stage["x"]
            sy = 44
            sw = CARD_WIDTH
            sh = 36

            count = sum(1 for n in self.nodes.values() if n.stage == stage["id"])
            dot_clr = stage_colors.get(stage["id"], (0.5, 0.5, 0.5))

            cr.save()
            cr.set_source_rgba(0.15, 0.16, 0.20, 0.90)
            self._rounded_rect(cr, sx, sy, sw, sh, 10)
            cr.fill()

            cr.set_source_rgba(1, 1, 1, 0.08)
            cr.set_line_width(1.0)
            self._rounded_rect(cr, sx, sy, sw, sh, 10)
            cr.stroke()

            # Colored stage dot
            cr.set_source_rgba(dot_clr[0], dot_clr[1], dot_clr[2], 0.95)
            cr.arc(sx + 18, sy + sh / 2, 4.5, 0, 2 * math.pi)
            cr.fill()

            # Title
            cr.set_source_rgba(0.92, 0.94, 0.98, 0.95)
            cr.set_font_size(11.5)
            cr.move_to(sx + 30, sy + 22)
            cr.show_text(stage["title"])

            # Count badge
            badge_text = str(count)
            cr.set_font_size(10)
            te = cr.text_extents(badge_text)
            bw = te.width + 12
            bx = sx + sw - bw - 10
            by = sy + 9
            bh = 18

            cr.set_source_rgba(1, 1, 1, 0.10)
            self._rounded_rect(cr, bx, by, bw, bh, 6)
            cr.fill()

            cr.set_source_rgba(0.80, 0.82, 0.88, 0.90)
            cr.move_to(bx + 6, by + 13)
            cr.show_text(badge_text)

            cr.restore()

    def _draw_card(self, cr, node: GraphNode):
        x, y, w, h = node.x, node.y, node.width, node.height
        alpha = node.current_alpha
        if node.dimmed:
            alpha *= 0.25

        # Drop shadow
        cr.save()
        cr.set_source_rgba(0, 0, 0, 0.35 * alpha)
        self._rounded_rect(cr, x + 2, y + 4, w, h, CARD_BORDER_R)
        cr.fill()
        cr.restore()

        # Card body
        cr.save()
        cr.set_source_rgba(CLR_CARD_BG[0], CLR_CARD_BG[1], CLR_CARD_BG[2], alpha)
        self._rounded_rect(cr, x, y, w, h, CARD_BORDER_R)
        cr.fill()

        # Border
        if node.selected:
            cr.set_source_rgba(CLR_CARD_SELECT[0], CLR_CARD_SELECT[1], CLR_CARD_SELECT[2], 0.35 * alpha)
            cr.set_line_width(6.0)
            self._rounded_rect(cr, x, y, w, h, CARD_BORDER_R)
            cr.stroke()

            cr.set_source_rgba(CLR_CARD_SELECT[0], CLR_CARD_SELECT[1], CLR_CARD_SELECT[2], 0.95 * alpha)
            cr.set_line_width(2.0)
        elif node.highlighted:
            cr.set_source_rgba(CLR_CARD_PATH[0], CLR_CARD_PATH[1], CLR_CARD_PATH[2], 0.85 * alpha)
            cr.set_line_width(1.5)
        else:
            cr.set_source_rgba(CLR_CARD_BDR[0], CLR_CARD_BDR[1], CLR_CARD_BDR[2], 0.70 * alpha)
            cr.set_line_width(1.0)

        self._rounded_rect(cr, x, y, w, h, CARD_BORDER_R)
        cr.stroke()
        cr.restore()

        # Left Icon Badge
        badge_x = x + 10
        badge_y = y + (h - 36) / 2
        badge_size = 36
        badge_r = 8

        cr.save()
        if node.stage == "channel":
            bg_clr = (0.20, 0.26, 0.36)
        elif node.stage == "mix":
            bg_clr = (0.28, 0.18, 0.28)
        else:
            bg_clr = (0.18, 0.28, 0.24)

        cr.set_source_rgba(bg_clr[0], bg_clr[1], bg_clr[2], 0.80 * alpha)
        self._rounded_rect(cr, badge_x, badge_y, badge_size, badge_size, badge_r)
        cr.fill()

        self._draw_vector_icon(cr, node.icon, badge_x + badge_size/2, badge_y + badge_size/2, alpha)
        cr.restore()

        # Text labels
        cr.save()
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(12)
        cr.set_source_rgba(CLR_TEXT_TITLE[0], CLR_TEXT_TITLE[1], CLR_TEXT_TITLE[2], 0.95 * alpha)
        cr.move_to(x + 54, y + 26)
        cr.show_text(node.label[:22])

        # Subtitle
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)
        cr.set_source_rgba(CLR_TEXT_SUB[0], CLR_TEXT_SUB[1], CLR_TEXT_SUB[2], 0.85 * alpha)
        cr.move_to(x + 54, y + 43)
        cr.show_text(node.subtitle[:28])

        # Lock indicator
        if node.locked:
            cr.set_font_size(10)
            cr.set_source_rgba(1, 1, 1, 0.5 * alpha)
            cr.move_to(x + w - 20, y + 20)
            cr.show_text("🔒")
        cr.restore()

        # Ports
        for p in node.inputs:
            px, py = p.get_canvas_pos()
            is_hovered = (p is self._hover_port)
            r = PORT_RADIUS + (2 if is_hovered else 0)
            cr.save()
            cr.set_source_rgba(0.24, 0.72, 0.88, 0.90 * alpha)
            cr.arc(px, py, r, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.40 * alpha)
            cr.set_line_width(1.0)
            cr.arc(px, py, r, 0, 2 * math.pi)
            cr.stroke()
            cr.restore()

        for p in node.outputs:
            px, py = p.get_canvas_pos()
            is_hovered = (p is self._hover_port)
            r = PORT_RADIUS + (2 if is_hovered else 0)
            cr.save()
            cr.set_source_rgba(0.85, 0.35, 0.82, 0.90 * alpha)
            cr.arc(px, py, r, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.40 * alpha)
            cr.set_line_width(1.0)
            cr.arc(px, py, r, 0, 2 * math.pi)
            cr.stroke()
            cr.restore()

    def _draw_vector_icon(self, cr, icon_type: str, cx: float, cy: float, alpha: float):
        cr.save()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        if icon_type == "mic":
            cr.set_source_rgba(0.40, 0.80, 1.00, 0.95 * alpha)
            cr.set_line_width(2.0)
            self._rounded_rect(cr, cx - 4, cy - 8, 8, 12, 4)
            cr.fill()
            cr.arc(cx, cy - 2, 7, 0, math.pi)
            cr.stroke()
            cr.move_to(cx, cy + 5)
            cr.line_to(cx, cy + 9)
            cr.stroke()
            cr.move_to(cx - 5, cy + 9)
            cr.line_to(cx + 5, cy + 9)
            cr.stroke()

        elif icon_type == "headphones":
            cr.set_source_rgba(0.85, 0.45, 0.95, 0.95 * alpha)
            cr.set_line_width(2.0)
            cr.arc(cx, cy - 2, 8, math.pi, 0)
            cr.stroke()
            self._rounded_rect(cr, cx - 10, cy - 3, 4, 8, 2)
            cr.fill()
            self._rounded_rect(cr, cx + 6, cy - 3, 4, 8, 2)
            cr.fill()

        elif icon_type == "speaker":
            cr.set_source_rgba(0.35, 0.90, 0.60, 0.95 * alpha)
            cr.set_line_width(1.8)
            cr.move_to(cx - 7, cy - 3)
            cr.line_to(cx - 4, cy - 3)
            cr.line_to(cx + 1, cy - 7)
            cr.line_to(cx + 1, cy + 7)
            cr.line_to(cx - 4, cy + 3)
            cr.line_to(cx - 7, cy + 3)
            cr.close_path()
            cr.fill()
            cr.arc(cx + 3, cy, 5, -math.pi/3, math.pi/3)
            cr.stroke()
            cr.arc(cx + 3, cy, 8, -math.pi/3, math.pi/3)
            cr.stroke()

        elif icon_type == "chrome":
            cr.set_source_rgba(0.95, 0.70, 0.20, 0.95 * alpha)
            cr.set_line_width(1.8)
            cr.arc(cx, cy, 8, 0, 2 * math.pi)
            cr.stroke()
            cr.arc(cx, cy, 3.5, 0, 2 * math.pi)
            cr.fill()
            cr.move_to(cx - 8, cy)
            cr.line_to(cx + 8, cy)
            cr.stroke()

        elif icon_type == "discord":
            cr.set_source_rgba(0.45, 0.60, 1.00, 0.95 * alpha)
            cr.set_line_width(1.8)
            self._rounded_rect(cr, cx - 8, cy - 6, 16, 12, 4)
            cr.stroke()
            cr.arc(cx - 3, cy - 1, 1.5, 0, 2 * math.pi)
            cr.fill()
            cr.arc(cx + 3, cy - 1, 1.5, 0, 2 * math.pi)
            cr.fill()

        elif icon_type == "spotify":
            cr.set_source_rgba(0.25, 0.88, 0.45, 0.95 * alpha)
            cr.set_line_width(2.0)
            cr.arc(cx, cy + 6, 12, -math.pi * 0.75, -math.pi * 0.25)
            cr.stroke()
            cr.arc(cx, cy + 4, 8, -math.pi * 0.75, -math.pi * 0.25)
            cr.stroke()
            cr.arc(cx, cy + 2, 4, -math.pi * 0.75, -math.pi * 0.25)
            cr.stroke()

        elif icon_type == "broadcast":
            cr.set_source_rgba(0.95, 0.40, 0.55, 0.95 * alpha)
            cr.set_line_width(1.8)
            cr.arc(cx, cy, 2.5, 0, 2 * math.pi)
            cr.fill()
            cr.arc(cx, cy, 6, -math.pi * 0.7, math.pi * 0.7)
            cr.stroke()
            cr.arc(cx, cy, 9, -math.pi * 0.7, math.pi * 0.7)
            cr.stroke()

        elif icon_type == "chat":
            cr.set_source_rgba(0.35, 0.75, 0.95, 0.95 * alpha)
            cr.set_line_width(1.8)
            self._rounded_rect(cr, cx - 7, cy - 6, 11, 8, 3)
            cr.stroke()
            self._rounded_rect(cr, cx - 2, cy - 1, 10, 7, 3)
            cr.fill()

        elif icon_type == "stream_deck":
            cr.set_source_rgba(0.70, 0.75, 0.85, 0.95 * alpha)
            for row in range(2):
                for col in range(3):
                    bx = cx - 7 + col * 5.5
                    by = cy - 5 + row * 5.5
                    self._rounded_rect(cr, bx, by, 4, 4, 1)
                    cr.fill()

        elif icon_type == "cam":
            cr.set_source_rgba(0.95, 0.55, 0.25, 0.95 * alpha)
            cr.set_line_width(1.8)
            self._rounded_rect(cr, cx - 7, cy - 5, 9, 10, 2)
            cr.fill()
            cr.move_to(cx + 3, cy - 4)
            cr.line_to(cx + 8, cy - 7)
            cr.line_to(cx + 8, cy + 7)
            cr.line_to(cx + 3, cy + 4)
            cr.close_path()
            cr.fill()

        elif icon_type == "tape":
            cr.set_source_rgba(0.85, 0.65, 0.35, 0.95 * alpha)
            cr.set_line_width(1.8)
            self._rounded_rect(cr, cx - 8, cy - 5, 16, 10, 2)
            cr.stroke()
            cr.arc(cx - 3.5, cy, 2.5, 0, 2 * math.pi)
            cr.stroke()
            cr.arc(cx + 3.5, cy, 2.5, 0, 2 * math.pi)
            cr.stroke()

        else:
            cr.set_source_rgba(0.70, 0.72, 0.80, 0.95 * alpha)
            cr.set_line_width(2.0)
            self._rounded_rect(cr, cx - 5, cy - 7, 10, 11, 2)
            cr.stroke()
            cr.move_to(cx, cy + 4)
            cr.line_to(cx, cy + 8)
            cr.stroke()

        cr.restore()

    def _get_connection_audio_peak(self, conn: Connection) -> float:
        """
        Determines the real-time audio signal peak (0.0 to 1.0+) flowing through this connection.
        Returns 0.0 if muted, silent, or no audio is actively playing.
        """
        if conn.muted:
            return 0.0

        pm = getattr(self.view, "peak_monitor", None)
        if not pm:
            return 0.0

        pwm = getattr(self.view, "pipewire_mgr", None)
        src_node = conn.src_port.node
        dst_node = conn.dst_port.node

        if conn.category == "ch_to_mix" or src_node.stage == "channel":
            ch_id = src_node.node_id.replace("ch_", "")
            mix_id = dst_node.node_id.replace("mix_", "")

            # Check PipeWire mixer volume and mute states
            if pwm:
                m_st = pwm.channel_master_states.get(ch_id, {})
                if m_st.get("muted", False) or m_st.get("volume", 80) == 0:
                    return 0.0
                ch_st = pwm.channel_states.get(ch_id, {}).get(mix_id, {})
                if not ch_st.get("enabled", True) or ch_st.get("muted", False) or ch_st.get("volume", 80) == 0:
                    return 0.0

            try:
                peak = pm.get_channel_peak(ch_id)
                if peak is None or peak < 0.005:
                    st_peaks = getattr(pm, "_channel_peaks", {}).get(ch_id, {})
                    peak = max(st_peaks.get("peak", 0.0), peak or 0.0)
                return float(peak or 0.0)
            except Exception:
                return 0.0

        elif conn.category == "mix_to_out" or src_node.stage == "mix":
            mix_id = src_node.node_id.replace("mix_", "")

            # Check PipeWire mix state
            if pwm:
                m_st = pwm.mix_states.get(mix_id, {})
                if m_st.get("muted", False) or m_st.get("volume", 100) == 0:
                    return 0.0

            try:
                peaks_dict = getattr(pm, "peaks", {})
                mix_data = (
                    peaks_dict.get(mix_id) or
                    peaks_dict.get(mix_id.replace("_mix", "")) or
                    peaks_dict.get(f"{mix_id}_mix")
                )
                if not mix_data and mix_id == "personal":
                    mix_data = peaks_dict.get("personal_mix")

                if isinstance(mix_data, dict):
                    return float(mix_data.get("peak", 0.0))
                return float(mix_data or 0.0)
            except Exception:
                return 0.0

        return 0.0

    def _draw_connection(self, cr, conn: Connection):
        alpha = conn.current_alpha
        rgb = conn.color_rgb

        lw = 2.6
        if conn.highlighted:
            lw = 3.6
        elif conn.hovered:
            lw = 3.2

        cr.save()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        if conn is self._selected_connection:
            cr.set_source_rgba(1, 1, 1, 0.40)
            cr.set_line_width(lw + 6)
            self._trace_connection_path(cr, conn)
            cr.stroke()

        audio_peak = self._get_connection_audio_peak(conn)
        is_flowing = audio_peak > 0.005

        if conn.highlighted:
            # Active glowing cable
            cr.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.95 * alpha)
            cr.set_line_width(lw)
            self._trace_connection_path(cr, conn)
            cr.stroke()

            # Signal Flow Pulse Overlay (only when active audio is playing!)
            if is_flowing:
                pulse_brightness = min(0.95, 0.45 + audio_peak * 0.50)
                cr.set_source_rgba(1.0, 1.0, 1.0, pulse_brightness * alpha)
                cr.set_line_width(lw * 0.75)
                dash_speed = 24.0 * (0.8 + min(1.2, audio_peak * 1.5))
                cr.set_dash([10.0, 14.0], -self._anim_phase * dash_speed)
                self._trace_connection_path(cr, conn)
                cr.stroke()

        elif self.selected_node is not None:
            # Dimmed inactive cable
            cr.set_source_rgba(CLR_CABLE_DIMMED[0], CLR_CABLE_DIMMED[1], CLR_CABLE_DIMMED[2], 0.12 * alpha)
            cr.set_line_width(1.5)
            self._trace_connection_path(cr, conn)
            cr.stroke()

        else:
            # Normal visible cable
            cr.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.72 * alpha)
            cr.set_line_width(lw)
            self._trace_connection_path(cr, conn)
            cr.stroke()

            # Signal Flow Pulse Overlay (only when active audio is playing!)
            if is_flowing:
                pulse_brightness = min(0.80, 0.25 + audio_peak * 0.50)
                cr.set_source_rgba(1.0, 1.0, 1.0, pulse_brightness * alpha)
                cr.set_line_width(lw * 0.6)
                dash_speed = 24.0 * (0.8 + min(1.2, audio_peak * 1.5))
                cr.set_dash([8.0, 16.0], -self._anim_phase * dash_speed)
                self._trace_connection_path(cr, conn)
                cr.stroke()

        cr.restore()

    def _draw_connection_preview(self, cr):
        port = self._connecting_from
        mx, my = self._connect_preview

        x0, y0 = port.get_canvas_pos()
        if port.direction == "out":
            x3, y3 = mx, my
        else:
            x3, y3 = x0, y0
            x0, y0 = mx, my

        cr.save()
        cr.set_source_rgba(1, 1, 1, 0.65)
        cr.set_line_width(2.5)
        cr.set_dash([8, 6], -self._anim_phase * 14.0)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        dx = max(abs(x3 - x0) * 0.45, 50)
        cr.move_to(x0, y0)
        cr.curve_to(x0 + dx, y0, x3 - dx, y3, x3, y3)
        cr.stroke()
        cr.restore()

    def _trace_connection_path(self, cr, conn):
        if self.line_style == LINE_STYLE_STRAIGHT:
            pts = conn.get_segments(orthogonal=True, nodes=self.nodes)
            if pts:
                cr.move_to(pts[0][0], pts[0][1])
                for pt in pts[1:]:
                    cr.line_to(pt[0], pt[1])
        else:
            x0, y0 = conn.src_port.get_canvas_pos()
            x3, y3 = conn.dst_port.get_canvas_pos()
            y0 += conn.src_offset
            y3 += conn.dst_offset
            dx = max(abs(x3 - x0) * 0.45, 50)
            cr.move_to(x0, y0)
            cr.curve_to(x0 + dx, y0, x3 - dx, y3, x3, y3)

    def _rounded_rect(self, cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi/2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi/2)
        cr.arc(x + r, y + h - r, r, math.pi/2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3*math.pi/2)
        cr.close_path()

    # ── Mouse Interaction ───────────────────────────────────────────────

    def _on_pressed(self, gesture, n_press, sx, sy):
        button = gesture.get_current_button()
        cx, cy = self.screen_to_canvas(sx, sy)

        state = gesture.get_current_event_state()
        if button == 2 or (button == 1 and (state & Gdk.ModifierType.CONTROL_MASK)):
            self._panning = True
            self._pan_start = (sx, sy)
            self._pan_start_offset = (self.pan_x, self.pan_y)
            return

        if button == 3:
            self._show_context_menu(sx, sy, cx, cy)
            return

        if button == 1:
            port = self.port_at(cx, cy)
            if port:
                self._connecting_from = port
                self._connecting_active = True
                self._connect_preview = (cx, cy)
                return

            node = self.node_at(cx, cy)
            if node:
                self._selected_connection = None
                if self.selected_node is node:
                    self.select_node(None)
                else:
                    self.select_node(node)
                return

            conn = self.connection_at(cx, cy)
            if conn:
                self._selected_connection = conn
                self.selected_node = None
                for n in self.nodes.values():
                    n.selected = False
                self.update_signal_path_isolation(None)
                if hasattr(self.view, "update_signal_strip"):
                    self.view.update_signal_strip(node=None, conn=conn)
                if hasattr(self.view, "btn_clear_sel"):
                    self.view.btn_clear_sel.set_visible(True)
                self._update_status(f"Selected Cable: {conn.src_port.node.label} → {conn.dst_port.node.label}")
                self.queue_draw()
                return

            self.select_node(None)
            self._selected_connection = None
            if hasattr(self.view, "update_signal_strip"):
                self.view.update_signal_strip(None, None)

    def _on_released(self, gesture, n_press, sx, sy):
        button = gesture.get_current_button()
        if button == 2 or (button == 1 and self._panning):
            self._panning = False
            return

        if button == 1 and self._connecting_active:
            cx, cy = self.screen_to_canvas(sx, sy)
            src_port = self._connecting_from
            dest_port = self.port_at(cx, cy)
            dest_node = self.node_at(cx, cy)

            if src_port and (dest_port or dest_node):
                target_node = dest_port.node if dest_port else dest_node

                # Channel -> Mix linking
                if src_port.node.stage == "channel" and target_node.stage == "mix":
                    ch_id = src_port.node.node_id.replace("ch_", "")
                    mix_id = target_node.node_id.replace("mix_", "")
                    log.info(f"[AudioFlow] Routing Channel {ch_id} to Mix {mix_id}")
                    if hasattr(self.view, "pipewire_mgr") and self.view.pipewire_mgr:
                        self.view.pipewire_mgr.set_channel_mix_enabled(ch_id, mix_id, True)
                        self.view._notify_topology_changed()
                        self._update_status(f"Linked {src_port.node.label} to {target_node.label}")

                # Mix -> Output routing
                elif src_port.node.stage == "mix" and target_node.stage == "output":
                    mix_id = src_port.node.node_id.replace("mix_", "")
                    out_node_id = target_node.node_id
                    target_device = "none" if out_node_id == "out_virtual_stream_target" else out_node_id.replace("out_", "")
                    log.info(f"[AudioFlow] Routing Mix {mix_id} to Output {target_device}")
                    if hasattr(self.view, "pipewire_mgr") and self.view.pipewire_mgr:
                        self.view.pipewire_mgr.update_mix(mix_id, target_device=target_device)
                        self.view._notify_topology_changed()
                        self._update_status(f"Routed {src_port.node.label} to {target_node.label}")

            self._connecting_active = False
            self._connecting_from = None
            self._connect_preview = None
            self.queue_draw()

    def _on_motion(self, controller, sx, sy):
        cx, cy = self.screen_to_canvas(sx, sy)

        if self._panning:
            dx = sx - self._pan_start[0]
            dy = sy - self._pan_start[1]
            self.pan_x = self._pan_start_offset[0] + dx
            self.pan_y = self._pan_start_offset[1] + dy
            self.queue_draw()
            return

        if self._connecting_active:
            self._connect_preview = (cx, cy)
            self.queue_draw()
            return

        # Hover states
        hover_port = self.port_at(cx, cy)
        hover_node = self.node_at(cx, cy)
        if hover_port != self._hover_port or hover_node != self._hover_node:
            self._hover_port = hover_port
            self._hover_node = hover_node
            self.queue_draw()

    def _on_drag_begin(self, gesture, start_x, start_y):
        state = gesture.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            return

        cx, cy = self.screen_to_canvas(start_x, start_y)
        if self.port_at(cx, cy):
            return

        node = self.node_at(cx, cy)
        if node and not node.locked and not self.global_lock:
            self._drag_node = node
            self._drag_start_node_pos = (node.x, node.y)
            self.set_cursor_from_name("grabbing")

    def _on_drag_update(self, gesture, offset_x, offset_y):
        if getattr(self, "_drag_node", None) and hasattr(self, "_drag_start_node_pos"):
            dx = offset_x / self.zoom
            dy = offset_y / self.zoom
            self._drag_node.x = self._drag_start_node_pos[0] + dx
            self._drag_node.y = self._drag_start_node_pos[1] + dy
            self.update_port_fanout()
            self.queue_draw()

    def _on_drag_end(self, gesture, offset_x, offset_y):
        if getattr(self, "_drag_node", None):
            self._drag_node = None
            self.set_cursor(None)
            self.update_port_fanout()
            self._schedule_save()
            self.queue_draw()

    def _on_scroll(self, controller, dx, dy):
        sx, sy = self.get_width() / 2, self.get_height() / 2
        cx, cy = self.screen_to_canvas(sx, sy)

        if dy < 0:
            self.zoom = min(self.zoom + ZOOM_STEP, ZOOM_MAX)
        elif dy > 0:
            self.zoom = max(self.zoom - ZOOM_STEP, ZOOM_MIN)

        self.pan_x = sx - cx * self.zoom
        self.pan_y = sy - cy * self.zoom

        self._update_zoom_label()
        self.queue_draw()
        return True

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            if self._connecting_active:
                self._connecting_active = False
                self._connecting_from = None
                self._connect_preview = None
                self.queue_draw()
                return True
            else:
                self.select_node(None)
                self._selected_connection = None
                if hasattr(self.view, "update_signal_strip"):
                    self.view.update_signal_strip(None, None)
                self.queue_draw()
                return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            if getattr(self, "_selected_connection", None):
                conn = self._selected_connection
                if conn.src_port.node.stage == "channel" and conn.dst_port.node.stage == "mix":
                    ch_id = conn.src_port.node.node_id.replace("ch_", "")
                    mix_id = conn.dst_port.node.node_id.replace("mix_", "")
                    log.info(f"[AudioFlow] Unlinking Channel {ch_id} from Mix {mix_id} via Delete key")
                    if hasattr(self.view, "pipewire_mgr") and self.view.pipewire_mgr:
                        self.view.pipewire_mgr.set_channel_mix_enabled(ch_id, mix_id, False)
                        self._selected_connection = None
                        self.view._notify_topology_changed()
                        self._update_status(f"Disconnected {conn.src_port.node.label} from {conn.dst_port.node.label}")
                        return True

        if keyval == Gdk.KEY_0 and (state & Gdk.ModifierType.CONTROL_MASK):
            self.zoom_fit()
            return True

        if keyval in (Gdk.KEY_plus, Gdk.KEY_equal):
            self.zoom_in()
            return True
        if keyval == Gdk.KEY_minus:
            self.zoom_out()
            return True

        return False

    # ── Context Menu & Confirmation Dialog ──────────────────────────────

    def _confirm_delete(self, heading: str, body: str, on_confirm):
        root_win = self.get_root()
        dialog = Adw.MessageDialog(
            transient_for=root_win if isinstance(root_win, Gtk.Window) else None,
            heading=heading,
            body=body
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def _on_response(d, response):
            if response == "delete":
                on_confirm()

        dialog.connect("response", _on_response)
        dialog.present()

    def _show_context_menu(self, sx, sy, cx, cy):
        popover = Gtk.Popover()
        popover.set_parent(self)
        popover.set_has_arrow(True)
        popover.add_css_class("wave-popover")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        def make_menu_btn(icon_name: str, label_text: str, on_click, destructive: bool = False):
            btn = Gtk.Button()
            btn.add_css_class("flat")
            if destructive:
                btn.add_css_class("destructive-action")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_margin_start(6)
            row.set_margin_end(6)
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(16)
            lbl = Gtk.Label(label=label_text)
            lbl.set_xalign(0)
            lbl.set_hexpand(True)
            row.append(img)
            row.append(lbl)
            btn.set_child(row)
            btn.connect("clicked", lambda b: (popover.popdown(), on_click()))
            return btn

        conn = self.connection_at(cx, cy)
        node = self.node_at(cx, cy)

        if conn and conn.src_port.node.stage == "channel" and conn.dst_port.node.stage == "mix":
            ch_id = conn.src_port.node.node_id.replace("ch_", "")
            mix_id = conn.dst_port.node.node_id.replace("mix_", "")
            def unlink_route():
                log.info(f"[AudioFlow] Disconnecting route {ch_id} -> {mix_id}")
                if hasattr(self.view, "pipewire_mgr") and self.view.pipewire_mgr:
                    self.view.pipewire_mgr.set_channel_mix_enabled(ch_id, mix_id, False)
                    self._selected_connection = None
                    self.view._notify_topology_changed()
                    self._update_status(f"Disconnected {conn.src_port.node.label} from {conn.dst_port.node.label}")
            box.append(make_menu_btn("edit-cut-symbolic", f"Disconnect Route ({conn.src_port.node.label} → {conn.dst_port.node.label})", unlink_route, destructive=True))

        if node:
            def toggle_lock():
                node.locked = not node.locked
                self._schedule_save()
                self.queue_draw()

            lock_icon = "changes-allow-symbolic" if node.locked else "changes-prevent-symbolic"
            lock_label = "Unlock Position" if node.locked else "Lock Position"
            box.append(make_menu_btn(lock_icon, lock_label, toggle_lock))

            def isolate():
                self.select_node(node)
            box.append(make_menu_btn("focus-windows-symbolic", "Isolate Signal Path", isolate))

            if node.stage == "channel":
                ch_id = node.node_id.replace("ch_", "")
                def delete_channel():
                    self._confirm_delete(
                        heading=f"Delete '{node.label}' Channel?",
                        body=f"Are you sure you want to delete the '{node.label}' channel? This will remove its channel strip and tear down its audio routing.",
                        on_confirm=lambda: (
                            self.view.pipewire_mgr.remove_channel(ch_id),
                            self.view._notify_topology_changed()
                        )
                    )
                box.append(make_menu_btn("user-trash-symbolic", f"Delete Channel '{node.label}'", delete_channel, destructive=True))

            elif node.stage == "mix":
                mix_id = node.node_id.replace("mix_", "")
                if hasattr(self.view, "mixer_view") and self.view.mixer_view and mix_id in self.view.mixer_view.mix_headers:
                    header = self.view.mixer_view.mix_headers[mix_id]
                    if hasattr(header, "gear_btn") and header.gear_btn.get_popover():
                        def open_mix_settings():
                            header.gear_btn.get_popover().popup()
                        box.append(make_menu_btn("preferences-system-symbolic", f"Edit Settings for '{node.label}'…", open_mix_settings))

                is_system_default = self.view.pipewire_mgr.is_mix_system_default(mix_id) if hasattr(self.view, "pipewire_mgr") and self.view.pipewire_mgr else False
                if mix_id not in ("personal_mix", "personal") and not is_system_default:
                    def delete_mix():
                        self._confirm_delete(
                            heading=f"Delete '{node.label}' Mix?",
                            body=f"Are you sure you want to delete the '{node.label}' submix? This will remove this mix bus from the mixer matrix.",
                            on_confirm=lambda: (
                                self.view.pipewire_mgr.remove_mix(mix_id),
                                self.view._notify_topology_changed()
                            )
                        )
                    box.append(make_menu_btn("user-trash-symbolic", f"Delete Mix '{node.label}'", delete_mix, destructive=True))

        box.append(make_menu_btn("view-refresh-symbolic", "Reset Layout", self.reset_layout))
        box.append(make_menu_btn("zoom-fit-best-symbolic", "Fit All Cards", self.zoom_fit))

        popover.set_child(box)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(sx), int(sy), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    # ── Zoom & Layout Controls ──────────────────────────────────────────

    def zoom_in(self):
        self.zoom = min(self.zoom + ZOOM_STEP, ZOOM_MAX)
        self._update_zoom_label()
        self.queue_draw()

    def zoom_out(self):
        self.zoom = max(self.zoom - ZOOM_STEP, ZOOM_MIN)
        self._update_zoom_label()
        self.queue_draw()

    def zoom_fit(self):
        if not self.nodes:
            return
        w = max(self.get_width(), 800)
        h = max(self.get_height(), 600)

        min_x = min(n.x for n in self.nodes.values())
        max_x = max(n.x + n.width for n in self.nodes.values())
        min_y = min(n.y for n in self.nodes.values())
        max_y = max(n.y + n.height for n in self.nodes.values())

        margin = 60
        span_x = (max_x - min_x) + margin * 2
        span_y = (max_y - min_y) + margin * 2

        scale_x = w / span_x if span_x > 0 else 1.0
        scale_y = h / span_y if span_y > 0 else 1.0

        self.zoom = max(ZOOM_MIN, min(min(scale_x, scale_y) * 0.92, ZOOM_MAX))

        cx = (min_x + max_x) / 2
        cy = (min_y + max_y) / 2
        self.pan_x = w / 2 - cx * self.zoom
        self.pan_y = h / 2 - cy * self.zoom

        self._update_zoom_label()
        self.queue_draw()

    def reset_layout(self):
        """Auto-arranges nodes neatly into the 3 canonical Audio Flow columns."""
        for stage_idx, stage in enumerate(STAGES):
            stage_nodes = [n for n in self.nodes.values() if n.stage == stage["id"]]
            start_y = 110
            spacing_y = 80
            if stage["id"] == "mix":
                start_y = 110
                spacing_y = 110
            elif stage["id"] == "output":
                start_y = 110
                spacing_y = 110

            for i, n in enumerate(stage_nodes):
                if not n.locked:
                    n.x = stage["x"]
                    n.y = start_y + i * spacing_y

        self.update_port_fanout()
        self._schedule_save()
        self.zoom_fit()
        self._update_status("Audio Flow layout organized")

    def search_filter(self, query: str):
        q = query.strip().lower()
        for n in self.nodes.values():
            n.dimmed = bool(q and q not in n.label.lower() and q not in n.subtitle.lower())
        self.queue_draw()

    def toggle_global_lock(self, locked: bool):
        self.global_lock = locked
        self._update_status("All cards locked" if locked else "Cards unlocked")
        self.queue_draw()

    def _update_zoom_label(self):
        if hasattr(self.view, "zoom_label"):
            pct = int(round(self.zoom * 100))
            self.view.zoom_label.set_label(f"{pct}%")

    def _update_status(self, msg: str):
        if hasattr(self.view, "status_label"):
            self.view.status_label.set_label(msg)

    def _schedule_save(self):
        if self._save_timer_id:
            GLib.source_remove(self._save_timer_id)
        self._save_timer_id = GLib.timeout_add(500, self._do_save)

    def _do_save(self):
        save_positions(self.nodes, self.connections, self.line_style, self.cable_gap)
        self._save_timer_id = None
        return False

    def flush_save(self):
        if self._save_timer_id:
            GLib.source_remove(self._save_timer_id)
            self._save_timer_id = None
        save_positions(self.nodes, self.connections, self.line_style, self.cable_gap)


# ═══════════════════════════════════════════════════════════════════════════
#  Audio Flow Container View (for WaveController MainWindow Stack)
# ═══════════════════════════════════════════════════════════════════════════

class AudioFlowView(Gtk.Box):
    """
    Complete Audio Flow view embedded inside WaveController's main stack.
    Includes top toolbar, Cairo canvas, and bottom status/legend bar.
    """

    def __init__(self, pipewire_mgr=None, hardware_mgr=None, peak_monitor=None, mixer_view=None, on_topology_changed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.pipewire_mgr = pipewire_mgr
        self.hardware_mgr = hardware_mgr
        self.peak_monitor = peak_monitor
        self.mixer_view = mixer_view
        self.on_topology_changed = on_topology_changed
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._build_toolbar()

        # Canvas & FX Inspector container
        self.canvas = AudioFlowCanvas(self)

        # FX Sidebar Slide-over Inspector
        self.fx_sidebar = FXSidebar(
            self.pipewire_mgr,
            on_closed=self._hide_fx_sidebar,
            on_fx_changed=self._on_fx_sidebar_changed,
        )
        self.fx_revealer = Gtk.Revealer()
        self.fx_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.fx_revealer.set_transition_duration(240)
        self.fx_revealer.set_child(self.fx_sidebar)
        self.fx_revealer.set_hexpand(False)
        self.fx_revealer.set_vexpand(True)
        self.fx_revealer.set_halign(Gtk.Align.END)
        self.fx_revealer.set_reveal_child(False)

        # Overlay canvas and FX Sidebar
        self.canvas_overlay = Gtk.Overlay()
        self.canvas_overlay.set_hexpand(True)
        self.canvas_overlay.set_vexpand(True)
        self.canvas_overlay.set_child(self.canvas)
        self.canvas_overlay.add_overlay(self.fx_revealer)

        self.append(self.canvas_overlay)

        self._build_status_bar()

        if self.mixer_view:
            self._connect_popovers()

        # Load topology
        self.refresh()
        self.connect("map", lambda w: self.refresh())

    def _show_fx_sidebar(self, channel_info):
        if (
            self.fx_revealer.get_reveal_child()
            and self.fx_sidebar.channel_info
            and self.fx_sidebar.channel_info.get("id") == channel_info.get("id")
        ):
            self._hide_fx_sidebar()
            return
        self.fx_sidebar.show_channel(channel_info)
        self.fx_revealer.set_reveal_child(True)

    def _hide_fx_sidebar(self):
        self.fx_revealer.set_reveal_child(False)

    def _on_fx_sidebar_changed(self, channel_id):
        # Update badge styling in signal strip if currently displayed
        if hasattr(self, "fx_strip_btn") and self.fx_strip_btn:
            self._update_strip_fx_badge(self.fx_strip_btn, channel_id)
        if hasattr(self, "mixer_view") and self.mixer_view:
            card = self.mixer_view.channel_cards.get(channel_id)
            if card:
                card.update_fx_state()

    def _update_strip_fx_badge(self, btn, channel_id):
        is_active = False
        if hasattr(self.pipewire_mgr, "fx_manager"):
            is_active = self.pipewire_mgr.fx_manager.is_fx_enabled(channel_id)
        if is_active:
            btn.set_label("FX")
            btn.add_css_class("active-fx")
            btn.remove_css_class("dimmed-fx")
            btn.set_tooltip_text("Audio Effects Active (Click to open FX Rack)")
        else:
            btn.set_label("FX")
            btn.add_css_class("dimmed-fx")
            btn.remove_css_class("active-fx")
            btn.set_tooltip_text("Audio Effects Bypassed (Click to open FX Rack)")

    def attach_mixer_view(self, mixer_view):
        self.mixer_view = mixer_view
        self._connect_popovers()

    def _connect_popovers(self):
        if not self.mixer_view:
            return
        if hasattr(self, "btn_add_channel") and hasattr(self.mixer_view, "setup_create_channel_popover"):
            self.mixer_view.setup_create_channel_popover(self.btn_add_channel, on_added_callback=self._notify_topology_changed)
        if hasattr(self, "btn_add_mix") and hasattr(self.mixer_view, "setup_create_mix_popover"):
            self.mixer_view.setup_create_mix_popover(self.btn_add_mix, on_added_callback=self._notify_topology_changed)

    def _notify_topology_changed(self):
        self.refresh()
        if callable(self.on_topology_changed):
            GLib.idle_add(self.on_topology_changed)

    def update_signal_strip(self, node=None, conn=None):
        if not hasattr(self, "signal_strip"):
            return

        # Clear existing children
        self.fx_strip_btn = None
        while self.signal_strip.get_first_child():
            self.signal_strip.remove(self.signal_strip.get_first_child())

        if not node and not conn:
            if hasattr(self, "sep_signal_strip"):
                self.sep_signal_strip.set_visible(False)
            self.signal_strip.set_visible(False)
            self._hide_fx_sidebar()
            return

        if hasattr(self, "sep_signal_strip"):
            self.sep_signal_strip.set_visible(True)
        self.signal_strip.set_visible(True)

        if node and node.stage == "mix":
            mix_id = node.node_id.replace("mix_", "")
            mix = next((m for m in self.pipewire_mgr.mixes if m["id"] == mix_id), None)
            if not mix:
                return

            m_type = mix.get("type", "sink")
            m_st = self.pipewire_mgr.mix_states.get(mix_id, {})
            vol = m_st.get("volume", 100)
            muted = m_st.get("muted", False)

            # Node Badge
            badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            badge.add_css_class("signal-strip-badge")
            badge.add_css_class("mix-badge")
            icon_name = "audio-headphones-symbolic" if m_type == "sink" else "audio-input-microphone-symbolic"
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(14)
            lbl = Gtk.Label(label=node.label)
            badge.append(img)
            badge.append(lbl)
            self.signal_strip.append(badge)

            # Target Output Dropdown (for sink mixes)
            if m_type == "sink":
                target_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                tgt_lbl = Gtk.Label(label="Target:")
                tgt_lbl.add_css_class("dim-label")
                target_box.append(tgt_lbl)

                target_options = [("none", "None (Virtual Only)"), ("default", "Default Output")]
                if self.hardware_mgr:
                    for d in self.hardware_mgr.get_tracked_output_devices():
                        k = d.get("device_key", d.get("name", ""))
                        d_name = d.get("display_name", d.get("name", "Audio Device"))
                        target_options.append((k, d_name))

                target_keys = [opt[0] for opt in target_options]
                target_labels = [opt[1] for opt in target_options]
                target_combo = Gtk.DropDown.new_from_strings(target_labels)

                curr_target = mix.get("target_device", "default" if mix_id in ("personal", "personal_mix") else "none")
                sel_idx = 0
                for i, k in enumerate(target_keys):
                    if k == curr_target or (curr_target and curr_target not in ("default", "none") and (k in curr_target or curr_target in k)):
                        sel_idx = i
                        break
                target_combo.set_selected(sel_idx)

                def on_target_changed(combo, *args):
                    idx = combo.get_selected()
                    if idx < len(target_keys):
                        new_tgt = target_keys[idx]
                        if new_tgt != mix.get("target_device"):
                            log.info(f"[SignalStrip] Changing target for mix {mix_id} to {new_tgt}")
                            self.pipewire_mgr.update_mix(mix_id, target_device=new_tgt)
                            self._notify_topology_changed()

                target_combo.connect("notify::selected", on_target_changed)
                target_box.append(target_combo)
                self.signal_strip.append(target_box)

            # Master Mix Level & Mute
            vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            mute_btn = Gtk.Button.new_from_icon_name("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic")
            mute_btn.add_css_class("flat")
            mute_btn.add_css_class("wave-icon-btn")
            mute_btn.set_tooltip_text("Unmute Mix" if muted else "Mute Mix")

            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            scale.set_value(vol)
            scale.set_size_request(80, -1)
            scale.set_draw_value(False)

            def on_mix_mute(b):
                new_m = self.pipewire_mgr.toggle_mix_master_mute(mix_id)
                mute_btn.set_icon_name("audio-volume-muted-symbolic" if new_m else "audio-volume-high-symbolic")
                self.refresh()

            def on_mix_vol(sc):
                self.pipewire_mgr.set_mix_master_volume(mix_id, int(sc.get_value()))

            mute_btn.connect("clicked", on_mix_mute)
            scale.connect("value-changed", on_mix_vol)
            vol_box.append(mute_btn)
            vol_box.append(scale)
            self.signal_strip.append(vol_box)

            # Edit Mix Settings button
            if hasattr(self, "mixer_view") and self.mixer_view and mix_id in self.mixer_view.mix_headers:
                header = self.mixer_view.mix_headers[mix_id]
                edit_btn = Gtk.MenuButton()
                edit_btn.add_css_class("flat")
                edit_btn.set_icon_name("preferences-system-symbolic")
                edit_btn.set_tooltip_text(f"Edit Settings for '{node.label}'")
                header._setup_edit_popover(edit_btn)
                self.signal_strip.append(edit_btn)

            # Delete Mix (for custom mixes)
            is_system_default = self.pipewire_mgr.is_mix_system_default(mix_id)
            if mix_id not in ("personal_mix", "personal") and not is_system_default:
                del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
                del_btn.add_css_class("flat")
                del_btn.add_css_class("destructive-action")
                del_btn.set_tooltip_text(f"Delete Mix '{node.label}'")
                def on_del_m(b):
                    self.canvas._confirm_delete(
                        heading=f"Delete '{node.label}' Mix?",
                        body=f"Are you sure you want to delete the '{node.label}' submix? This will remove this mix bus from the mixer matrix.",
                        on_confirm=lambda: (
                            self.pipewire_mgr.remove_mix(mix_id),
                            self._notify_topology_changed()
                        )
                    )
                del_btn.connect("clicked", on_del_m)
                self.signal_strip.append(del_btn)

        elif node and node.stage == "channel":
            ch_id = node.node_id.replace("ch_", "")
            ch = next((c for c in self.pipewire_mgr.channels if c["id"] == ch_id), None)
            if not ch:
                return

            ch_type = ch.get("type", "app")
            is_mic = ch_type in ("source", "mic") or "mic" in ch_id.lower() or "wave" in ch_id.lower()
            is_group = ch_type == "group"
            is_virtual = ch_type == "virtual" or "desktop" in ch_id.lower()

            m_st = self.pipewire_mgr.channel_master_states.get(ch_id, {})
            vol = m_st.get("volume", 80)
            muted = m_st.get("muted", False)

            # Node Badge
            badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            badge.add_css_class("signal-strip-badge")
            badge.add_css_class("channel-badge")
            icon_name = "audio-input-microphone-symbolic" if is_mic else ("folder-symbolic" if is_group else ("audio-card-symbolic" if is_virtual else "applications-multimedia-symbolic"))
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(14)
            lbl = Gtk.Label(label=node.label)
            badge.append(img)
            badge.append(lbl)
            self.signal_strip.append(badge)

            # Hardware Pre-Amp Controls (ONLY for hardware mic / Wave device)
            if is_mic and self.hardware_mgr and getattr(self.hardware_mgr, "is_connected", False):
                hw_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                gain_val = self.hardware_mgr.get_gain_db() if hasattr(self.hardware_mgr, "get_gain_db") else 46.0
                gain_lbl = Gtk.Label(label=f"Gain: {gain_val:.0f}dB")
                gain_lbl.add_css_class("dim-label")
                hw_box.append(gain_lbl)

                gain_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 75, 1)
                gain_scale.set_value(gain_val)
                gain_scale.set_size_request(80, -1)
                gain_scale.set_draw_value(False)

                def on_gain_changed(sc):
                    g = sc.get_value()
                    gain_lbl.set_text(f"Gain: {g:.0f}dB")
                    if hasattr(self.hardware_mgr, "set_gain_db"):
                        self.hardware_mgr.set_gain_db(g)

                gain_scale.connect("value-changed", on_gain_changed)
                hw_box.append(gain_scale)

                if hasattr(self.hardware_mgr, "get_phantom_power") and hasattr(self.hardware_mgr, "set_phantom_power"):
                    btn_48v = Gtk.ToggleButton(label="48V")
                    is_48v = bool(self.hardware_mgr.get_phantom_power())
                    btn_48v.set_active(is_48v)
                    btn_48v.add_css_class("flat")
                    btn_48v.add_css_class("wave-48v-badge")
                    btn_48v.set_valign(Gtk.Align.CENTER)
                    if is_48v:
                        btn_48v.add_css_class("active-48v")
                    else:
                        btn_48v.add_css_class("dimmed-48v")
                    btn_48v.set_tooltip_text("48V Phantom Power")
                    def on_48v_toggled(b):
                        act = b.get_active()
                        if act:
                            b.add_css_class("active-48v")
                            b.remove_css_class("dimmed-48v")
                        else:
                            b.add_css_class("dimmed-48v")
                            b.remove_css_class("active-48v")
                        self.hardware_mgr.set_phantom_power(act)
                    btn_48v.connect("toggled", on_48v_toggled)
                    hw_box.append(btn_48v)

                if hasattr(self.hardware_mgr, "get_clipguard") and hasattr(self.hardware_mgr, "set_clipguard"):
                    btn_clip = Gtk.ToggleButton(label="Clipguard")
                    btn_clip.set_active(self.hardware_mgr.get_clipguard())
                    btn_clip.add_css_class("flat")
                    btn_clip.set_valign(Gtk.Align.CENTER)
                    btn_clip.set_tooltip_text("Elgato Clipguard")
                    btn_clip.connect("toggled", lambda b: self.hardware_mgr.set_clipguard(b.get_active()))
                    hw_box.append(btn_clip)

                self.signal_strip.append(hw_box)

            # Channel Master Volume & Mute
            vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            mute_btn = Gtk.Button.new_from_icon_name("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic")
            mute_btn.add_css_class("flat")
            mute_btn.add_css_class("wave-icon-btn")
            mute_btn.set_tooltip_text("Unmute Channel" if muted else "Mute Channel")

            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            scale.set_value(vol)
            scale.set_size_request(80, -1)
            scale.set_draw_value(False)

            def on_ch_mute(b):
                new_m = self.pipewire_mgr.toggle_channel_master_mute(ch_id)
                mute_btn.set_icon_name("audio-volume-muted-symbolic" if new_m else "audio-volume-high-symbolic")
                self.refresh()

            def on_ch_vol(sc):
                self.pipewire_mgr.set_channel_master_volume(ch_id, int(sc.get_value()))

            mute_btn.connect("clicked", on_ch_mute)
            scale.connect("value-changed", on_ch_vol)
            vol_box.append(mute_btn)
            vol_box.append(scale)
            self.signal_strip.append(vol_box)

            # FX Button: STRICTLY ONLY FOR MIC / INPUT CHANNELS!
            if is_mic:
                fx_btn = Gtk.Button(label="FX")
                fx_btn.add_css_class("flat")
                fx_btn.add_css_class("wave-fx-badge")
                fx_btn.set_valign(Gtk.Align.CENTER)
                self._update_strip_fx_badge(fx_btn, ch_id)
                self.fx_strip_btn = fx_btn

                def on_fx_clicked(b):
                    self._show_fx_sidebar(ch)
                fx_btn.connect("clicked", on_fx_clicked)
                self.signal_strip.append(fx_btn)

            # Delete / Remove Channel Button (for custom groups, apps, virtual channels)
            if ch_id not in ("mic",):
                del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
                del_btn.add_css_class("flat")
                del_btn.add_css_class("destructive-action")
                del_btn.set_tooltip_text(f"Delete Channel '{node.label}'")
                def on_del_c(b):
                    self.canvas._confirm_delete(
                        heading=f"Delete '{node.label}' Channel?",
                        body=f"Are you sure you want to delete the '{node.label}' channel? This will remove its channel strip and tear down its audio routing.",
                        on_confirm=lambda: (
                            self.pipewire_mgr.remove_channel(ch_id),
                            self._notify_topology_changed()
                        )
                    )
                del_btn.connect("clicked", on_del_c)
                self.signal_strip.append(del_btn)

        elif node and node.stage == "output":
            badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            badge.add_css_class("signal-strip-badge")
            badge.add_css_class("output-badge")
            is_virtual = "virtual" in node.node_id
            icon_name = "audio-headphones-symbolic" if not is_virtual else "audio-input-microphone-symbolic"
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(14)
            lbl = Gtk.Label(label=node.label)
            badge.append(img)
            badge.append(lbl)
            self.signal_strip.append(badge)

            # Hardware Headphone / Output Volume (if physical output)
            if not is_virtual and self.hardware_mgr:
                hp_vol = self.hardware_mgr.get_headphone_volume() if hasattr(self.hardware_mgr, "get_headphone_volume") else 65
                hp_muted = self.hardware_mgr.get_headphone_mute() if hasattr(self.hardware_mgr, "get_headphone_mute") else False

                out_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                mute_btn = Gtk.Button.new_from_icon_name("audio-volume-muted-symbolic" if hp_muted else "audio-volume-high-symbolic")
                mute_btn.add_css_class("flat")
                mute_btn.add_css_class("wave-icon-btn")
                mute_btn.set_tooltip_text("Mute Headphone Output")

                scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
                scale.set_value(hp_vol)
                scale.set_size_request(80, -1)
                scale.set_draw_value(False)

                def on_hp_mute(b):
                    if hasattr(self.hardware_mgr, "toggle_headphone_mute"):
                        new_m = self.hardware_mgr.toggle_headphone_mute()
                        mute_btn.set_icon_name("audio-volume-muted-symbolic" if new_m else "audio-volume-high-symbolic")

                def on_hp_vol(sc):
                    if hasattr(self.hardware_mgr, "set_headphone_volume"):
                        self.hardware_mgr.set_headphone_volume(int(sc.get_value()))

                mute_btn.connect("clicked", on_hp_mute)
                scale.connect("value-changed", on_hp_vol)
                out_box.append(mute_btn)
                out_box.append(scale)
                self.signal_strip.append(out_box)

        elif conn:
            src_node = conn.src_port.node
            dst_node = conn.dst_port.node
            if src_node.stage == "channel" and dst_node.stage == "mix":
                ch_id = src_node.node_id.replace("ch_", "")
                mix_id = dst_node.node_id.replace("mix_", "")

                st = self.pipewire_mgr.get_channel_state(ch_id, mix_id)
                vol = st.get("volume", 80)
                muted = st.get("muted", False)

                badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                badge.add_css_class("signal-strip-badge")
                badge.add_css_class("link-badge")
                img = Gtk.Image.new_from_icon_name("insert-link-symbolic")
                img.set_pixel_size(14)
                lbl = Gtk.Label(label=f"{src_node.label} → {dst_node.label}")
                badge.append(img)
                badge.append(lbl)
                self.signal_strip.append(badge)

                # Send Level Fader & Mute
                vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                mute_btn = Gtk.Button.new_from_icon_name("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic")
                mute_btn.add_css_class("flat")
                mute_btn.set_tooltip_text("Mute Send")

                scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
                scale.set_value(vol)
                scale.set_size_request(80, -1)
                scale.set_draw_value(False)

                def on_send_mute(b):
                    new_m = self.pipewire_mgr.toggle_channel_mute(ch_id, mix_id)
                    mute_btn.set_icon_name("audio-volume-muted-symbolic" if new_m else "audio-volume-high-symbolic")
                    self.refresh()

                def on_send_vol(sc):
                    self.pipewire_mgr.set_channel_volume(ch_id, mix_id, int(sc.get_value()))

                mute_btn.connect("clicked", on_send_mute)
                scale.connect("value-changed", on_send_vol)
                vol_box.append(mute_btn)
                vol_box.append(scale)
                self.signal_strip.append(vol_box)

                # Disconnect button
                disc_btn = Gtk.Button()
                disc_btn.add_css_class("flat")
                disc_btn.add_css_class("destructive-action")
                disc_btn.set_tooltip_text("Disconnect Route")
                disc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                disc_icon = Gtk.Image.new_from_icon_name("edit-cut-symbolic")
                disc_icon.set_pixel_size(14)
                disc_lbl = Gtk.Label(label="Disconnect")
                disc_row.append(disc_icon)
                disc_row.append(disc_lbl)
                disc_btn.set_child(disc_row)
                def on_disc_clicked(b):
                    self.pipewire_mgr.set_channel_mix_enabled(ch_id, mix_id, False)
                    self.canvas._selected_connection = None
                    self._notify_topology_changed()
                disc_btn.connect("clicked", on_disc_clicked)
                self.signal_strip.append(disc_btn)

    def _build_toolbar(self):
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("graph-toolbar")
        toolbar.set_margin_start(14)
        toolbar.set_margin_end(14)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(6)
        toolbar.set_size_request(-1, 46)

        # Add Channel Node Button
        self.btn_add_channel = Gtk.MenuButton()
        self.btn_add_channel.set_valign(Gtk.Align.CENTER)
        self.btn_add_channel.set_tooltip_text("Add Audio Channel (Apps, Inputs, Virtual, Groups)")
        self.btn_add_channel.add_css_class("flat")
        self.btn_add_channel.add_css_class("wave-action-btn")
        add_ch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_ch_box.set_valign(Gtk.Align.CENTER)
        add_ch_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        add_ch_icon.set_pixel_size(16)
        add_ch_lbl = Gtk.Label(label="Add Channel")
        add_ch_box.append(add_ch_icon)
        add_ch_box.append(add_ch_lbl)
        self.btn_add_channel.set_child(add_ch_box)
        toolbar.append(self.btn_add_channel)

        # Add Mix Node Button
        self.btn_add_mix = Gtk.MenuButton()
        self.btn_add_mix.set_valign(Gtk.Align.CENTER)
        self.btn_add_mix.set_tooltip_text("Add Audio Mix (Virtual Source or Output Sink)")
        self.btn_add_mix.add_css_class("flat")
        self.btn_add_mix.add_css_class("wave-action-btn")
        add_mix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_mix_box.set_valign(Gtk.Align.CENTER)
        add_mix_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        add_mix_icon.set_pixel_size(16)
        add_mix_lbl = Gtk.Label(label="Add Mix")
        add_mix_box.append(add_mix_icon)
        add_mix_box.append(add_mix_lbl)
        self.btn_add_mix.set_child(add_mix_box)
        toolbar.append(self.btn_add_mix)

        sep_btns = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep_btns.set_valign(Gtk.Align.CENTER)
        sep_btns.set_size_request(-1, 20)
        sep_btns.set_margin_start(4)
        sep_btns.set_margin_end(4)
        toolbar.append(sep_btns)

        # Search filter
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_valign(Gtk.Align.CENTER)
        self.search_entry.set_placeholder_text("Filter audio nodes…")
        self.search_entry.set_size_request(210, -1)
        self.search_entry.connect("search-changed", lambda e: self.canvas.search_filter(e.get_text()))
        toolbar.append(self.search_entry)

        # Clear selection button (Strictly symbolic icon)
        self.btn_clear_sel = Gtk.Button()
        self.btn_clear_sel.set_valign(Gtk.Align.CENTER)
        self.btn_clear_sel.add_css_class("clear-sel-btn")
        self.btn_clear_sel.set_tooltip_text("Clear signal path isolation (Esc)")
        self.btn_clear_sel.set_visible(False)
        sel_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sel_box.set_valign(Gtk.Align.CENTER)
        sel_icon = Gtk.Image.new_from_icon_name("edit-clear-symbolic")
        sel_icon.set_pixel_size(14)
        sel_lbl = Gtk.Label(label="Clear selection")
        sel_box.append(sel_icon)
        sel_box.append(sel_lbl)
        self.btn_clear_sel.set_child(sel_box)
        def on_clear_selection(_):
            self.canvas.select_node(None)
            self.canvas._selected_connection = None
            self.update_signal_strip(None, None)
            self.canvas.queue_draw()
        self.btn_clear_sel.connect("clicked", on_clear_selection)
        toolbar.append(self.btn_clear_sel)

        sep_style = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep_style.set_valign(Gtk.Align.CENTER)
        sep_style.set_size_request(-1, 20)
        sep_style.set_margin_start(4)
        sep_style.set_margin_end(4)
        toolbar.append(sep_style)

        # Line style toggle
        style_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        style_box.set_valign(Gtk.Align.CENTER)
        style_box.add_css_class("line-style-box")

        self._style_buttons = {}
        btn_bezier = Gtk.ToggleButton(label="Curves")
        btn_bezier.set_valign(Gtk.Align.CENTER)
        btn_bezier.set_active(True)
        btn_bezier.add_css_class("active-style")
        btn_bezier.set_tooltip_text("Smooth flowing audio cables")
        btn_bezier.connect("toggled", self._on_line_style_toggled, LINE_STYLE_BEZIER)
        style_box.append(btn_bezier)
        self._style_buttons[LINE_STYLE_BEZIER] = btn_bezier

        btn_manual = Gtk.ToggleButton(label="Orthogonal")
        btn_manual.set_valign(Gtk.Align.CENTER)
        btn_manual.set_tooltip_text("Clean right-angle cables")
        btn_manual.connect("toggled", self._on_line_style_toggled, LINE_STYLE_STRAIGHT)
        style_box.append(btn_manual)
        self._style_buttons[LINE_STYLE_STRAIGHT] = btn_manual
        toolbar.append(style_box)

        # Signal Strip — Contextual property bar for active node/cable
        self.sep_signal_strip = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self.sep_signal_strip.set_valign(Gtk.Align.CENTER)
        self.sep_signal_strip.set_size_request(-1, 20)
        self.sep_signal_strip.set_margin_start(4)
        self.sep_signal_strip.set_margin_end(4)
        self.sep_signal_strip.set_visible(False)
        toolbar.append(self.sep_signal_strip)

        self.signal_strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.signal_strip.set_valign(Gtk.Align.CENTER)
        self.signal_strip.add_css_class("signal-strip")
        self.signal_strip.set_visible(False)
        toolbar.append(self.signal_strip)

        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        toolbar.append(spacer)

        # Zoom controls
        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        zoom_box.set_valign(Gtk.Align.CENTER)
        btn_zoom_out = Gtk.Button.new_from_icon_name("zoom-out-symbolic")
        btn_zoom_out.set_valign(Gtk.Align.CENTER)
        btn_zoom_out.add_css_class("flat")
        btn_zoom_out.set_tooltip_text("Zoom Out (−)")
        btn_zoom_out.connect("clicked", lambda _: self.canvas.zoom_out())
        zoom_box.append(btn_zoom_out)

        self.zoom_label = Gtk.Label(label="100%")
        self.zoom_label.set_valign(Gtk.Align.CENTER)
        self.zoom_label.add_css_class("zoom-label")
        zoom_box.append(self.zoom_label)

        btn_zoom_in = Gtk.Button.new_from_icon_name("zoom-in-symbolic")
        btn_zoom_in.set_valign(Gtk.Align.CENTER)
        btn_zoom_in.add_css_class("flat")
        btn_zoom_in.set_tooltip_text("Zoom In (+)")
        btn_zoom_in.connect("clicked", lambda _: self.canvas.zoom_in())
        zoom_box.append(btn_zoom_in)

        btn_zoom_fit = Gtk.Button.new_from_icon_name("zoom-fit-best-symbolic")
        btn_zoom_fit.set_valign(Gtk.Align.CENTER)
        btn_zoom_fit.add_css_class("flat")
        btn_zoom_fit.set_tooltip_text("Fit All Nodes")
        btn_zoom_fit.connect("clicked", lambda _: self.canvas.zoom_fit())
        zoom_box.append(btn_zoom_fit)
        toolbar.append(zoom_box)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_valign(Gtk.Align.CENTER)
        sep.set_size_request(-1, 20)
        sep.set_margin_start(4)
        sep.set_margin_end(4)
        toolbar.append(sep)

        # Lock toggle
        self.lock_btn = Gtk.ToggleButton()
        self.lock_btn.set_valign(Gtk.Align.CENTER)
        self.lock_btn.set_icon_name("changes-prevent-symbolic")
        self.lock_btn.add_css_class("flat")
        self.lock_btn.set_tooltip_text("Lock All Node Positions")
        self.lock_btn.connect("toggled", self._on_lock_toggled)
        toolbar.append(self.lock_btn)

        # Refresh topology
        btn_reload = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        btn_reload.set_valign(Gtk.Align.CENTER)
        btn_reload.add_css_class("flat")
        btn_reload.set_tooltip_text("Refresh Audio Flow from PipeWire")
        btn_reload.connect("clicked", lambda _: self.refresh())
        toolbar.append(btn_reload)

        self.append(toolbar)

    def _build_status_bar(self):
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        status_bar.add_css_class("graph-status-bar")
        status_bar.set_margin_start(14)
        status_bar.set_margin_end(14)
        status_bar.set_margin_top(4)
        status_bar.set_margin_bottom(6)

        self.status_label = Gtk.Label(label="Ready — Click any node to isolate its routing while dimming everything else.")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_hexpand(True)
        status_bar.append(self.status_label)

        # Color Legend
        legend_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        for color_hex, text in [
            ("#e879f9", "Channel → Mix"),
            ("#4ade80", "Mix → Output"),
            ("#fb923c", "Monitor"),
            ("#ef4444", "Muted"),
            ("#64748b", "Not in path"),
        ]:
            lbl = Gtk.Label()
            lbl.set_markup(f'<span foreground="{color_hex}">●</span> {text}')
            lbl.add_css_class("legend-item")
            legend_box.append(lbl)

        status_bar.append(legend_box)

        # Node / Link count
        self.stats_lbl = Gtk.Label(label="")
        self.stats_lbl.set_xalign(1)
        status_bar.append(self.stats_lbl)

        self.append(status_bar)

    def refresh(self):
        """Reloads the live graph from PipeWire and updates counts."""
        self.canvas.load_topology(self.pipewire_mgr, self.hardware_mgr)
        node_count = len(self.canvas.nodes)
        conn_count = len(self.canvas.connections)
        self.stats_lbl.set_label(f"{node_count} cards · {conn_count} links")

    def _on_line_style_toggled(self, btn, style):
        if getattr(self, "_updating_style", False):
            return
        if not btn.get_active():
            if self.canvas.line_style == style:
                btn.set_active(True)
            return

        self._updating_style = True
        for s, b in self._style_buttons.items():
            if s != style:
                b.set_active(False)
                b.remove_css_class("active-style")
        btn.add_css_class("active-style")
        self._updating_style = False

        self.canvas.line_style = style
        self.canvas.queue_draw()

    def _on_lock_toggled(self, btn):
        locked = btn.get_active()
        if locked:
            btn.set_icon_name("changes-allow-symbolic")
            btn.add_css_class("lock-active")
        else:
            btn.set_icon_name("changes-prevent-symbolic")
            btn.remove_css_class("lock-active")
        self.canvas.toggle_global_lock(locked)

    def cleanup(self):
        """Flushes positions to disk on shutdown."""
        if hasattr(self, "canvas") and self.canvas:
            self.canvas.flush_save()
