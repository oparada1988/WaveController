#!/usr/bin/env bash
# ==============================================================================
# WaveController — Linux Multi-Track Mixer & Elgato Hardware Management
# Installation, Upgrade, and Uninstallation Script
# ==============================================================================

set -eo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Standard XDG Paths
INSTALL_DIR="${HOME}/.local/share/wavecontroller"
BIN_DIR="${HOME}/.local/bin"
DESKTOP_DIR="${HOME}/.local/share/applications"
ICON_PNG_DIR="${HOME}/.local/share/icons/hicolor/512x512/apps"
ICON_SVG_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
WIREPLUMBER_CONF_DIR="${HOME}/.config/wireplumber/wireplumber.conf.d"
CONFIG_DIR="${HOME}/.config/WaveController"
AUTOSTART_DIR="${HOME}/.config/autostart"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || pwd)"
if [ -f "${SCRIPT_DIR}/main.py" ] && [ -d "${SCRIPT_DIR}/wavecontroller" ]; then
    REPO_DIR="${SCRIPT_DIR}"
elif [ -f "${SCRIPT_DIR}/../main.py" ] && [ -d "${SCRIPT_DIR}/../wavecontroller" ]; then
    REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
    REPO_DIR=""
fi

# Ensure repository source directory exists (auto-fetches from GitHub if running via curl | bash)
ensure_repo_dir() {
    if [ -n "${REPO_DIR}" ] && [ -f "${REPO_DIR}/main.py" ] && [ -d "${REPO_DIR}/wavecontroller" ]; then
        return 0
    fi

    echo -e "${BLUE}Downloading latest WaveController release from GitHub...${NC}"
    local temp_dir
    temp_dir="$(mktemp -d -t wavecontroller-install-XXXXXX)"

    # Clean up temporary directory upon script termination or completion
    trap 'rm -rf "${temp_dir}"' EXIT INT TERM

    if command -v git &>/dev/null; then
        git clone --depth 1 https://github.com/oparada1988/WaveController.git "${temp_dir}/WaveController" --quiet
        REPO_DIR="${temp_dir}/WaveController"
    else
        curl -fsSL https://github.com/oparada1988/WaveController/archive/refs/heads/main.tar.gz | tar -xz -C "${temp_dir}"
        REPO_DIR="${temp_dir}/WaveController-main"
    fi

    if [ ! -f "${REPO_DIR}/main.py" ] || [ ! -d "${REPO_DIR}/wavecontroller" ]; then
        echo -e "${RED}[ERROR] Failed to download and extract WaveController repository from GitHub.${NC}"
        exit 1
    fi
}

# Banner
print_banner() {
    echo -e "${CYAN}${BOLD}"
    echo "=================================================================="
    echo "       WaveController - Linux Audio Mixer & Hardware Manager      "
    echo "=================================================================="
    echo -e "${NC}"
}

# Help Menu
show_help() {
    print_banner
    echo -e "${BOLD}Usage:${NC} ./install.sh [OPTION]"
    echo ""
    echo -e "${BOLD}Options:${NC}"
    echo -e "  ${GREEN}--install, -i${NC}        Install WaveController, services, desktop launcher, icons, and udev rules (default)"
    echo -e "  ${BLUE}--upgrade, -u${NC}        Update WaveController to latest version while preserving settings"
    echo -e "  ${RED}--uninstall, -r${NC}      Remove WaveController, systemd service, desktop icons, and launcher wrapper"
    echo -e "  ${YELLOW}--autostart${NC}          Enable systemd user service for background launch on desktop login"
    echo -e "  ${YELLOW}--disable-autostart${NC}  Disable systemd background auto-start service"
    echo -e "  ${CYAN}--help, -h${NC}             Show this help message and exit"
    echo ""
    echo -e "${BOLD}Installation Paths:${NC}"
    echo -e "  - Application:    ${INSTALL_DIR}"
    echo -e "  - CLI Launcher:   ${BIN_DIR}/wavecontroller"
    echo -e "  - Systemd Unit:   ${SYSTEMD_USER_DIR}/wavecontroller.service"
    echo -e "  - WirePlumber:    ${WIREPLUMBER_CONF_DIR}/51-wavecontroller-wave-xlr.conf"
    echo -e "  - Desktop Entry:  ${DESKTOP_DIR}/com.oparada.WaveController.desktop"
    echo -e "  - Config State:   ${CONFIG_DIR}/config.json"
    echo ""
}

# Verify System & Python Dependencies
check_dependencies() {
    echo -e "${BLUE}[1/6] Checking system dependencies and Python environment...${NC}"
    local missing_bins=()
    local missing_py=()

    for bin_cmd in python3 pipewire wireplumber pw-link pw-loopback wpctl rsync udevadm; do
        if ! command -v "${bin_cmd}" &>/dev/null; then
            missing_bins+=("${bin_cmd}")
        fi
    done

    # Check Python version >= 3.10
    if command -v python3 &>/dev/null; then
        local py_ver_ok
        py_ver_ok=$(python3 -c "import sys; print(1 if sys.version_info >= (3, 10) else 0)" 2>/dev/null || echo 0)
        if [ "${py_ver_ok}" -ne 1 ]; then
            missing_bins+=("python3>=3.10")
        fi

        # Check Python GI, Gtk 4.0, Adw 1, and Pillow
        if ! python3 -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); from gi.repository import Gtk, Adw, GLib, Gio" &>/dev/null; then
            missing_py+=("PyGObject / GTK4 / Libadwaita (gi, Gtk 4.0, Adw 1)")
        fi
        if ! python3 -c "import PIL" &>/dev/null; then
            missing_py+=("Pillow (PIL)")
        fi
    fi

    if [ ${#missing_bins[@]} -ne 0 ] || [ ${#missing_py[@]} -ne 0 ]; then
        echo -e "${RED}[ERROR] Missing required dependencies:${NC}"
        for b in "${missing_bins[@]}"; do
            echo -e "  - Binary: ${b}"
        done
        for p in "${missing_py[@]}"; do
            echo -e "  - Python: ${p}"
        done
        echo ""
        echo -e "${YELLOW}Please install the missing dependencies for your Linux distribution:${NC}"

        local distro=""
        if [ -f /etc/os-release ]; then
            distro=$(source /etc/os-release 2>/dev/null && echo "${ID} ${ID_LIKE}" || true)
        fi

        case "${distro}" in
            *ubuntu*|*debian*|*pop*|*mint*)
                echo -e "${BOLD}Ubuntu / Debian:${NC}"
                echo "  sudo apt update && sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-pil pipewire wireplumber pipewire-bin rsync"
                ;;
            *fedora*|*rhel*|*centos*)
                echo -e "${BOLD}Fedora / RHEL:${NC}"
                echo "  sudo dnf install -y python3-gobject gtk4 libadwaita python3-pillow pipewire wireplumber rsync"
                ;;
            *arch*|*manjaro*)
                echo -e "${BOLD}Arch Linux:${NC}"
                echo "  sudo pacman -S --needed python-gobject gtk4 libadwaita python-pillow pipewire wireplumber rsync"
                ;;
            *suse*|*opensuse*)
                echo -e "${BOLD}openSUSE:${NC}"
                echo "  sudo zypper install -y python3-gobject gtk4 typelib-1_0-Adw-1 python3-Pillow pipewire wireplumber rsync"
                ;;
            *)
                echo -e "${BOLD}Required packages:${NC}"
                echo "  Python 3 (>=3.10), PyGObject, GTK 4, Libadwaita 1, Pillow, PipeWire, WirePlumber, pipewire-tools (pw-link, pw-loopback), rsync"
                ;;
        esac
        echo ""
        exit 1
    fi

    echo -e "${GREEN}[OK] All required system dependencies and Python libraries are available.${NC}"
}

# Install / Update Udev Rules & Boot Pre-Init Helper
install_udev_rules() {
    ensure_repo_dir
    echo -e "${BLUE}[2/6] Checking hardware USB permissions & boot pre-init helper (udev rules)...${NC}"
    local udev_file="${REPO_DIR}/data/99-elgato-wave.rules"
    local udev_dest="/etc/udev/rules.d/99-elgato-wave.rules"
    local init_script="${REPO_DIR}/scripts/wavecontroller_hw_init.py"
    local init_dest="/usr/local/bin/wavecontroller-hw-init"

    if [ -f "${init_script}" ]; then
        if [ ! -f "${init_dest}" ] || ! cmp -s "${init_script}" "${init_dest}"; then
            echo -e "${YELLOW}Installing WaveController hardware boot pre-init helper (sudo password required)...${NC}"
            sudo cp "${init_script}" "${init_dest}"
            sudo chmod +x "${init_dest}"
        fi
    fi

    if [ -f "${udev_file}" ]; then
        if [ ! -f "${udev_dest}" ] || ! cmp -s "${udev_file}" "${udev_dest}"; then
            echo -e "${YELLOW}Installing Elgato Wave hardware udev rules...${NC}"
            sudo cp "${udev_file}" "${udev_dest}"
            sudo udevadm control --reload-rules
            sudo udevadm trigger
            echo -e "${GREEN}[OK] udev rules & boot pre-init helper installed and activated successfully.${NC}"
        else
            echo -e "${GREEN}[OK] Hardware udev rules are up to date.${NC}"
        fi
    else
        echo -e "${YELLOW}Downloading latest udev rules from GitHub...${NC}"
        curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/data/99-elgato-wave.rules | sudo tee "${udev_dest}" > /dev/null
        sudo udevadm control --reload-rules
        sudo udevadm trigger
        echo -e "${GREEN}[OK] udev rules installed and activated successfully.${NC}"
    fi
}

# Install Core Files & Bundled DSP Plugins
install_core_files() {
    echo -e "${BLUE}[3/6] Installing application package to ${INSTALL_DIR}...${NC}"
    mkdir -p "${INSTALL_DIR}"
    mkdir -p "${BIN_DIR}"
    mkdir -p "${DESKTOP_DIR}"
    mkdir -p "${ICON_PNG_DIR}"
    mkdir -p "${ICON_SVG_DIR}"

    # Sync codebase
    if [ -d "${REPO_DIR}/wavecontroller" ]; then
        rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
            "${REPO_DIR}/main.py" \
            "${REPO_DIR}/requirements.txt" \
            "${REPO_DIR}/wavecontroller" \
            "${REPO_DIR}/assets" \
            "${REPO_DIR}/data" \
            "${INSTALL_DIR}/"

        # Install bundled core audio DSP plugins to ~/.ladspa
        mkdir -p "${HOME}/.ladspa"
        if [ -d "${REPO_DIR}/wavecontroller/assets/plugins" ]; then
            cp -n "${REPO_DIR}/wavecontroller/assets/plugins"/*.so "${HOME}/.ladspa/" 2>/dev/null || true
        fi
    else
        echo -e "${RED}[ERROR] Repository source directory not found.${NC}"
        exit 1
    fi

    # Create CLI Wrapper in ~/.local/bin
    echo -e "${BLUE}Creating executable wrapper in ${BIN_DIR}/wavecontroller...${NC}"
    cat << 'EOF' > "${BIN_DIR}/wavecontroller"
#!/usr/bin/env bash
export PATH="${HOME}/.local/bin:${PATH}"
exec /usr/bin/python3 "${HOME}/.local/share/wavecontroller/main.py" "$@"
EOF
    chmod +x "${BIN_DIR}/wavecontroller"

    # Install Desktop Icons
    echo -e "${BLUE}Installing desktop application icons...${NC}"
    # Clean up legacy/broken scalable app icons
    rm -f "${HOME}/.local/share/icons/hicolor/scalable/apps/com.oparada.WaveController.svg"
    rm -f "${HOME}/.local/share/icons/hicolor/scalable/apps/WaveController.svg"

    src_png=""
    if [ -f "${INSTALL_DIR}/assets/icons/com.oparada.WaveController.png" ]; then
        src_png="${INSTALL_DIR}/assets/icons/com.oparada.WaveController.png"
    elif [ -f "${INSTALL_DIR}/assets/icons/WaveController.png" ]; then
        src_png="${INSTALL_DIR}/assets/icons/WaveController.png"
    fi

    if [ -n "${src_png}" ]; then
        for res in 48 64 128 256 512; do
            res_dir="${HOME}/.local/share/icons/hicolor/${res}x${res}/apps"
            mkdir -p "${res_dir}"
            python3 -c "from PIL import Image; img = Image.open('${src_png}').resize((${res}, ${res}), Image.Resampling.LANCZOS); img.save('${res_dir}/com.oparada.WaveController.png'); img.save('${res_dir}/WaveController.png')" 2>/dev/null || {
                cp "${src_png}" "${res_dir}/com.oparada.WaveController.png"
                cp "${src_png}" "${res_dir}/WaveController.png"
            }
        done
    fi

    mkdir -p "${HOME}/.local/share/icons/hicolor/scalable/apps"
    mkdir -p "${HOME}/.local/share/icons/hicolor/symbolic/apps"
    mkdir -p "${HOME}/.local/share/icons/hicolor/scalable/status"
    mkdir -p "${HOME}/.local/share/icons/hicolor/symbolic/status"
    mkdir -p "${HOME}/.local/share/icons/hicolor/scalable/panel"
    mkdir -p "${HOME}/.local/share/icons/hicolor/22x22/status"

    for sym_file in "${INSTALL_DIR}"/assets/icons/*-symbolic.svg; do
        if [ -f "${sym_file}" ]; then
            cp "${sym_file}" "${HOME}/.local/share/icons/hicolor/scalable/apps/"
            cp "${sym_file}" "${HOME}/.local/share/icons/hicolor/symbolic/apps/"
            cp "${sym_file}" "${HOME}/.local/share/icons/hicolor/scalable/status/"
            cp "${sym_file}" "${HOME}/.local/share/icons/hicolor/symbolic/status/"
        fi
    done
    for svg_file in "${INSTALL_DIR}"/assets/icons/*.svg; do
        if [ -f "${svg_file}" ]; then
            base_svg=$(basename "${svg_file}")
            if [ "${base_svg}" != "WaveController.svg" ] && [ "${base_svg}" != "com.oparada.WaveController.svg" ]; then
                cp "${svg_file}" "${HOME}/.local/share/icons/hicolor/scalable/apps/"
            fi
        fi
    done
    for tray_file in "${INSTALL_DIR}"/assets/icons/*tray*; do
        if [ -f "${tray_file}" ]; then
            cp "${tray_file}" "${HOME}/.local/share/icons/hicolor/scalable/status/" 2>/dev/null || true
            cp "${tray_file}" "${HOME}/.local/share/icons/hicolor/symbolic/status/" 2>/dev/null || true
            cp "${tray_file}" "${HOME}/.local/share/icons/hicolor/scalable/panel/" 2>/dev/null || true
            cp "${tray_file}" "${HOME}/.local/share/icons/hicolor/22x22/status/" 2>/dev/null || true
        fi
    done

    if command -v gtk-update-icon-cache &>/dev/null; then
        gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" 2>/dev/null || true
    fi
    if command -v kbuildsycoca6 &>/dev/null; then
        kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
    elif command -v kbuildsycoca5 &>/dev/null; then
        kbuildsycoca5 --noincremental >/dev/null 2>&1 || true
    fi

    # Create Desktop Launcher
    echo -e "${BLUE}Creating desktop menu launcher in ${DESKTOP_DIR}...${NC}"
    cat << EOF > "${DESKTOP_DIR}/com.oparada.WaveController.desktop"
[Desktop Entry]
Name=WaveController
GenericName=Audio Mixer
Comment=Elgato Wave Link & Advanced Multi-Track Virtual Mixer for Linux
Exec=${BIN_DIR}/wavecontroller
Icon=com.oparada.WaveController
Terminal=false
Type=Application
Categories=AudioVideo;Audio;Mixer;GTK;
StartupWMClass=com.oparada.WaveController
Keywords=audio;mixer;wave;elgato;pipewire;volume;
EOF
    chmod +x "${DESKTOP_DIR}/com.oparada.WaveController.desktop"

    # Refresh Desktop and Icon Databases
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "${DESKTOP_DIR}" >/dev/null 2>&1 || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true
    fi
}

# Deploy WirePlumber Studio Audio Profile (anti-suspend, 48kHz clock pinning)
install_wireplumber_config() {
    echo -e "${BLUE}[4/6] Installing WirePlumber studio audio configuration...${NC}"
    mkdir -p "${WIREPLUMBER_CONF_DIR}"
    local wp_conf="${REPO_DIR}/data/51-wavecontroller-wave-xlr.conf"
    if [ -f "${wp_conf}" ]; then
        cp "${wp_conf}" "${WIREPLUMBER_CONF_DIR}/51-wavecontroller-wave-xlr.conf"
        echo -e "${GREEN}[OK] WirePlumber 48kHz clock-lock and anti-suspend profile installed.${NC}"
    elif [ -f "${INSTALL_DIR}/data/51-wavecontroller-wave-xlr.conf" ]; then
        cp "${INSTALL_DIR}/data/51-wavecontroller-wave-xlr.conf" "${WIREPLUMBER_CONF_DIR}/51-wavecontroller-wave-xlr.conf"
        echo -e "${GREEN}[OK] WirePlumber 48kHz clock-lock and anti-suspend profile installed.${NC}"
    fi
}

# Install Systemd User Service Unit
install_systemd_service() {
    echo -e "${BLUE}[5/6] Installing background daemon systemd service unit...${NC}"
    # Remove leftover pre-systemd XDG autostart entry
    rm -f "${AUTOSTART_DIR}/com.oparada.WaveController.desktop"

    mkdir -p "${SYSTEMD_USER_DIR}"
    cat << EOF > "${SYSTEMD_USER_DIR}/wavecontroller.service"
[Unit]
Description=WaveController Audio Routing Daemon
Documentation=https://github.com/oparada1988/WaveController
After=graphical-session.target pipewire.service wireplumber.service
Wants=pipewire.service wireplumber.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=${BIN_DIR}/wavecontroller --daemon
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical-session.target
EOF
    systemctl --user daemon-reload 2>/dev/null || true
    echo -e "${GREEN}[OK] Systemd user service unit installed to ${SYSTEMD_USER_DIR}/wavecontroller.service.${NC}"
}

# Finalize Installation
finalize_install() {
    echo -e "${BLUE}[6/6] Finalizing installation...${NC}"
    echo ""
    echo -e "${GREEN}${BOLD}[OK] WaveController installed successfully!${NC}"
    echo -e "Launch it from your application menu or run: ${CYAN}wavecontroller${NC}"
    echo -e "To enable background startup on login, run: ${CYAN}./install.sh --autostart${NC}"
    echo ""
}

# Upgrade Installation
upgrade_app() {
    print_banner
    echo -e "${BLUE}Upgrading WaveController to latest version...${NC}"
    check_dependencies
    ensure_repo_dir

    # If inside git repo, pull latest and install pre-commit regression guard
    if [ -d "${REPO_DIR}/.git" ]; then
        echo -e "${CYAN}Pulling latest changes from git repository...${NC}"
        cd "${REPO_DIR}"
        git pull origin main
        if [ -f "${REPO_DIR}/scripts/pre-commit-hook.sh" ]; then
            mkdir -p "${REPO_DIR}/.git/hooks"
            cp "${REPO_DIR}/scripts/pre-commit-hook.sh" "${REPO_DIR}/.git/hooks/pre-commit"
            chmod +x "${REPO_DIR}/.git/hooks/pre-commit"
        fi
    fi

    install_udev_rules
    install_core_files
    install_wireplumber_config
    install_systemd_service
    echo -e "${GREEN}${BOLD}[OK] Upgrade complete! Saved configurations were preserved.${NC}"
}

# Autostart Management (user-space systemd unit, no sudo required)
enable_autostart() {
    install_systemd_service
    systemctl --user daemon-reload
    systemctl --user enable --now wavecontroller.service
    echo -e "${GREEN}[OK] WaveController background auto-start enabled via systemd user service.${NC}"
}

disable_autostart() {
    rm -f "${AUTOSTART_DIR}/com.oparada.WaveController.desktop"
    systemctl --user stop wavecontroller.service 2>/dev/null || true
    systemctl --user disable wavecontroller.service 2>/dev/null || true
    rm -f "${SYSTEMD_USER_DIR}/wavecontroller.service" 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
    echo -e "${YELLOW}[OK] WaveController background auto-start disabled.${NC}"
}

# Uninstallation
uninstall_app() {
    print_banner
    echo -e "${YELLOW}Uninstalling WaveController...${NC}"

    # 1. Terminate running processes
    pkill -f "python3.*wavecontroller/main.py" || true
    pkill -f "pw-loopback.*WaveController" || true
    disable_autostart >/dev/null 2>&1 || true

    # 2. Remove files
    rm -rf "${INSTALL_DIR}"
    rm -f "${BIN_DIR}/wavecontroller"
    sudo rm -f "/usr/local/bin/wavecontroller-hw-init" 2>/dev/null || true
    rm -f "${WIREPLUMBER_CONF_DIR}/51-wavecontroller-wave-xlr.conf"
    rm -f "${DESKTOP_DIR}/com.oparada.WaveController.desktop"
    rm -f "${AUTOSTART_DIR}/com.oparada.WaveController.desktop"
    rm -f "${ICON_PNG_DIR}/com.oparada.WaveController.png"
    rm -f "${ICON_SVG_DIR}/com.oparada.WaveController.svg"

    # Refresh caches
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "${DESKTOP_DIR}" >/dev/null 2>&1 || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true
    fi

    echo -e "${GREEN}[OK] Application, systemd service, desktop entry, and icons removed.${NC}"

    # Prompt for configuration removal
    if [ -d "${CONFIG_DIR}" ]; then
        read -p "Do you also want to remove your saved settings (~/.config/WaveController)? [y/N]: " confirm
        if [[ "$confirm" =~ ^[Yy]$ ]]; then
            rm -rf "${CONFIG_DIR}"
            echo -e "${GREEN}[OK] User configuration purged.${NC}"
        else
            echo -e "${CYAN}Configuration files preserved at ${CONFIG_DIR}.${NC}"
        fi
    fi
    echo ""
    echo -e "${GREEN}${BOLD}[OK] Uninstallation complete.${NC}"
}

# Main Execution Routing
case "$1" in
    --install|-i|"")
        print_banner
        check_dependencies
        ensure_repo_dir
        install_udev_rules
        install_core_files
        install_wireplumber_config
        install_systemd_service
        finalize_install
        ;;
    --upgrade|-u)
        upgrade_app
        ;;
    --uninstall|-r)
        uninstall_app
        ;;
    --autostart)
        enable_autostart
        ;;
    --disable-autostart)
        disable_autostart
        ;;
    --help|-h)
        show_help
        ;;
    *)
        echo -e "${RED}Unknown option: $1${NC}"
        show_help
        exit 1
        ;;
esac
