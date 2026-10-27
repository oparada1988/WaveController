Name:           wavecontroller
Version:        0.0.4.3
Release:        1%{?dist}
Summary:        Elgato Wave Link & Advanced Multi-Track Virtual Mixer for Linux
License:        GPL-3.0-or-later
URL:            https://github.com/oparada1988/WaveController
Source0:        wavecontroller-%{version}.tar.gz
BuildArch:      x86_64

# Define systemd userunitdir if not provided by macros
%{!?_userunitdir: %global _userunitdir %{_prefix}/lib/systemd/user}

# Disable debuginfo package generation
%define debug_package %{nil}
%define _enable_debug_packages 0
%define __strip /bin/true

Requires:       python3 >= 3.10
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       python3-pillow
Requires:       libusb1
Requires:       pipewire
Requires:       pipewire-utils
Requires:       wireplumber
Requires:       systemd-udev

%description
WaveController provides hardware DSP control for Elgato Wave devices (Wave XLR,
Wave:3, Wave:1) and a full virtual multi-track audio routing mixer using
PipeWire and WirePlumber. Features include submixing, zero-latency monitoring,
rotary dial synchronization, and studio audio profiles.

%prep
%setup -q

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_datadir}/wavecontroller
mkdir -p %{buildroot}%{_datadir}/applications
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/512x512/apps
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps
mkdir -p %{buildroot}%{_prefix}/lib/udev/rules.d
mkdir -p %{buildroot}%{_userunitdir}
mkdir -p %{buildroot}%{_datadir}/wireplumber/wireplumber.conf.d
mkdir -p %{buildroot}%{_prefix}/lib/ladspa

# Launcher wrapper
cat << 'EOF' > %{buildroot}%{_bindir}/wavecontroller
#!/bin/sh
exec /usr/bin/python3 /usr/share/wavecontroller/main.py "$@"
EOF
chmod 0755 %{buildroot}%{_bindir}/wavecontroller

# Hardware pre-init script
install -m 0755 scripts/wavecontroller_hw_init.py %{buildroot}%{_bindir}/wavecontroller-hw-init

# Core application files
cp -r main.py requirements.txt wavecontroller assets data %{buildroot}%{_datadir}/wavecontroller/

# Bundled LADSPA plugins
if [ -d wavecontroller/assets/plugins ]; then
    install -m 0755 wavecontroller/assets/plugins/*.so %{buildroot}%{_prefix}/lib/ladspa/ 2>/dev/null || true
fi

# Desktop entry
install -m 0644 packaging/common/com.oparada.WaveController.desktop %{buildroot}%{_datadir}/applications/

# Desktop icons
if [ -f assets/icons/com.oparada.WaveController.png ]; then
    install -m 0644 assets/icons/com.oparada.WaveController.png %{buildroot}%{_datadir}/icons/hicolor/512x512/apps/com.oparada.WaveController.png
fi
for f in assets/icons/*-symbolic.svg; do
    [ -f "$f" ] && install -m 0644 "$f" %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/
done
for f in assets/icons/*.svg; do
    [ -f "$f" ] && install -m 0644 "$f" %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/
done

# Systemd user service unit
install -m 0644 packaging/common/wavecontroller.service %{buildroot}%{_userunitdir}/wavecontroller.service

# WirePlumber studio profile
install -m 0644 data/51-wavecontroller-wave-xlr.conf %{buildroot}%{_datadir}/wireplumber/wireplumber.conf.d/51-wavecontroller-wave-xlr.conf

# Udev rules
install -m 0644 packaging/common/99-elgato-wave.rules %{buildroot}%{_prefix}/lib/udev/rules.d/99-elgato-wave.rules

%post
if [ ! -e /usr/local/bin/wavecontroller-hw-init ] && [ -d /usr/local/bin ]; then
    ln -sf /usr/bin/wavecontroller-hw-init /usr/local/bin/wavecontroller-hw-init || true
fi
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=usb 2>/dev/null || true
touch --no-create %{_datadir}/icons/hicolor &>/dev/null || :
gtk-update-icon-cache %{_datadir}/icons/hicolor &>/dev/null || :
update-desktop-database &>/dev/null || :

%postun
if [ $1 -eq 0 ]; then
    if [ -L /usr/local/bin/wavecontroller-hw-init ]; then
        rm -f /usr/local/bin/wavecontroller-hw-init || true
    fi
    udevadm control --reload-rules 2>/dev/null || true
    touch --no-create %{_datadir}/icons/hicolor &>/dev/null || :
    gtk-update-icon-cache %{_datadir}/icons/hicolor &>/dev/null || :
    update-desktop-database &>/dev/null || :
fi

%files
%{_bindir}/wavecontroller
%{_bindir}/wavecontroller-hw-init
%{_datadir}/wavecontroller
%{_datadir}/applications/com.oparada.WaveController.desktop
%{_datadir}/icons/hicolor/*/apps/*
%{_userunitdir}/wavecontroller.service
%{_datadir}/wireplumber/wireplumber.conf.d/51-wavecontroller-wave-xlr.conf
%{_prefix}/lib/udev/rules.d/99-elgato-wave.rules
%{_prefix}/lib/ladspa/*.so

%changelog
* Tue Oct 27 2026 Oscar Parada <oparada1988@gmail.com> - 0.0.4.3-1
- Release v0.0.4.3 (Pre-Alpha): Keyboard shortcuts dialog, mixer QoL, IPC controls, CPU optimizations

* Sat Oct 17 2026 Oscar Parada <oparada1988@gmail.com> - 0.0.4.2-1
- Release v0.0.4.2 (Pre-Alpha): Audio Flow Signal Strip, embedded FX rack, and packaging
