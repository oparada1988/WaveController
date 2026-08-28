# WaveController Technical Roadmap & Milestones

This document tracks the technical milestones and core engine enhancements for WaveController. Live milestone tracking is also available on [GitHub Milestones](https://github.com/oparada1988/WaveController/milestones).

---

## Milestone 1: UI Rendering and Visual Performance Optimization
**Focus**: Delivering a smooth 60 FPS visual experience, zero-lock drawing routines, and responsive layout scaling across all display configurations.
* **GitHub Milestone**: [Milestone 1](https://github.com/oparada1988/WaveController/milestone/1)

### Key Objectives
* [ ] **60 FPS Hardware-Accelerated Rendering**: Transition VU peak polling to GTK tick callbacks (`Gtk.Widget.add_tick_callback`) with delta-time smoothing.
* [ ] **Zero-Lock UI Thread Isolation**: Decouple audio thread mutexes from GTK rendering routines to eliminate micro-stutters during heavy audio loads.
* [ ] **Responsive Grid and Window Scaling**: Dynamic layout scaling across single and multi-monitor setups without clipping faders.
* [ ] **High-Precision Stereo Sliders**: Enhanced custom-drawn Cairo/GTK sliders with high-DPI sub-pixel accuracy and zero slider drift.

---

## Milestone 2: Resilient USB Device Lifecycle and Hotplug Engine
**Focus**: Resilient USB hardware handling, non-blocking communications, and automatic state recovery upon device reconnection or system resume.
* **GitHub Milestone**: [Milestone 2](https://github.com/oparada1988/WaveController/milestone/2)

### Key Objectives
* [ ] **Asynchronous Non-Blocking USB I/O**: Dedicated hardware worker thread ensuring slow USB HID responses never delay the mixer interface.
* [ ] **Zero-Crash Hotplug and Auto-Reconnection**: Gracefully detect hardware disconnects and automatically restore gain, 48V phantom power, low-cut filters, Clipguard, and LED ring settings within 200ms of reconnection.
* [ ] **In-App Visual Connection State**: Non-intrusive notification banners for hardware disconnect and reconnect events.
* [ ] **Sleep and Resume Synchronization**: Re-initialize USB endpoints and reset device registers immediately upon system wake.

---

## Milestone 3: High-Speed Zero-Latency Routing and Matrix Orchestration
**Focus**: Ultra-fast submix creation, single-pass PipeWire graph synchronization, and self-healing link persistence.
* **GitHub Milestone**: [Milestone 3](https://github.com/oparada1988/WaveController/milestone/3)

### Key Objectives
* [ ] **Parallel Batch Submix Provisioning**: Batch loopback initialization reducing channel and mix creation time to under 100ms.
* [ ] **Single-Pass PipeWire Graph Diffing**: Unified set operations for port mapping to eliminate link-flipping and connection thrashing.
* [ ] **Instant Application Stream Interception**: Event-driven stream detection to link newly opened applications before audio playback begins.
* [ ] **WirePlumber Restart Self-Healing**: Automatically re-establish all matrix links if PipeWire or WirePlumber restarts in the background.

---

## Milestone 4: Integrated Visual PipeWire Routing Graph and Patchbay
**Focus**: An interactive, real-time visual routing graph built directly into WaveController for complete signal flow visibility.
* **GitHub Milestone**: [Milestone 4](https://github.com/oparada1988/WaveController/milestone/4)

### Key Objectives
* [ ] **Interactive Visual Canvas**: Dedicated tab displaying live signal flow from Applications to Ingestion Channels, Submix Matrix Faders, Mix Buses, and Physical Outputs.
* [ ] **Live Signal Flow Visuals**: Animated patch cables with color-coded stream indicators for active playback, mix buses, and microphones.
* [ ] **Real-Time Node Telemetry**: Embedded mini-VU meters and volume indicators directly on graph node blocks.
* [ ] **Signal Flow Diagnostics**: Click any node or link to inspect PipeWire properties, port IDs, buffer latency, and format specifications.
