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
CONFIG_DIR="${HOME}/.config/WaveController"
AUTOSTART_DIR="${HOME}/.config/autostart"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/main.py" ]; then
    REPO_DIR="${SCRIPT_DIR}"
else
    REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi

# Banner
print_banner() {
    echo -e "${CYAN}${BOLD}"
    echo "=================================================================="
    echo "       WaveController — Linux Audio Mixer & Hardware Manager      "
    echo "=================================================================="
    echo -e "${NC}"
}

# Help Menu
show_help() {
    print_banner
    echo -e "${BOLD}Usage:${NC} ./install.sh [OPTION]"
    echo ""
    echo -e "${BOLD}Options:${NC}"
    echo -e "  ${GREEN}--install, -i${NC}        Install WaveController, desktop launcher, icons, and udev rules (default)"
    echo -e "  ${BLUE}--upgrade, -u${NC}        Update WaveController to the latest release while preserving settings"
    echo -e "  ${RED}--uninstall, -r${NC}      Remove WaveController, desktop icons, and launcher wrapper"
    echo -e "  ${YELLOW}--autostart${NC}          Enable systemd user service for background launch on desktop login"
    echo -e "  ${YELLOW}--disable-autostart${NC}  Disable systemd background auto-start service"
    echo -e "  ${CYAN}--help, -h${NC}             Show this help message and exit"
    echo ""
    echo -e "${BOLD}Installation Paths:${NC}"
    echo -e "  • Application:   ${INSTALL_DIR}"
    echo -e "  • CLI Launcher:  ${BIN_DIR}/wavecontroller"
    echo -e "  • Desktop Entry: ${DESKTOP_DIR}/com.oparada.WaveController.desktop"
    echo -e "  • Config State:  ${CONFIG_DIR}/config.json"
    echo ""
}

# Install / Update Udev Rules
install_udev_rules() {
    echo -e "${BLUE}[1/5] Checking hardware USB permissions (udev rules)...${NC}"
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
            echo -e "${GREEN}✔ udev rules & boot pre-init helper installed and activated successfully.${NC}"
        else
            echo -e "${GREEN}✔ Hardware udev rules are up to date.${NC}"
        fi
    else
        echo -e "${YELLOW}Downloading latest udev rules from GitHub...${NC}"
        curl -fsSL https://raw.githubusercontent.com/oparada1988/WaveController/main/data/99-elgato-wave.rules | sudo tee "${udev_dest}" > /dev/null
        sudo udevadm control --reload-rules
        sudo udevadm trigger
        echo -e "${GREEN}✔ udev rules installed and activated successfully.${NC}"
    fi
}

# Install Core Files
install_core_files() {
    echo -e "${BLUE}[2/5] Installing application package to ${INSTALL_DIR}...${NC}"
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
    else
        echo -e "${RED}Error: Repository source directory not found.${NC}"
        exit 1
    fi

    # Create CLI Wrapper in ~/.local/bin
    echo -e "${BLUE}[3/5] Creating executable wrapper in ${BIN_DIR}/wavecontroller...${NC}"
    cat << 'EOF' > "${BIN_DIR}/wavecontroller"
#!/usr/bin/env bash
export PATH="${HOME}/.local/bin:${PATH}"
exec /usr/bin/python3 "${HOME}/.local/share/wavecontroller/main.py" "$@"
EOF
    chmod +x "${BIN_DIR}/wavecontroller"

    # Install Desktop Icons
    echo -e "${BLUE}[4/5] Installing desktop application icons...${NC}"
    if [ -f "${INSTALL_DIR}/assets/icons/com.oparada.WaveController.png" ]; then
        cp "${INSTALL_DIR}/assets/icons/com.oparada.WaveController.png" "${ICON_PNG_DIR}/com.oparada.WaveController.png"
    elif [ -f "${INSTALL_DIR}/assets/icons/WaveController.png" ]; then
        cp "${INSTALL_DIR}/assets/icons/WaveController.png" "${ICON_PNG_DIR}/com.oparada.WaveController.png"
    fi

    if [ -f "${INSTALL_DIR}/assets/icons/com.oparada.WaveController-tray.svg" ]; then
        cp "${INSTALL_DIR}/assets/icons/com.oparada.WaveController-tray.svg" "${ICON_SVG_DIR}/com.oparada.WaveController.svg"
    elif [ -f "${INSTALL_DIR}/assets/icons/wctray.svg" ]; then
        cp "${INSTALL_DIR}/assets/icons/wctray.svg" "${ICON_SVG_DIR}/com.oparada.WaveController.svg"
    fi

    if [ -f "${INSTALL_DIR}/assets/icons/wavecontroller-tray-symbolic.svg" ]; then
        cp "${INSTALL_DIR}/assets/icons/wavecontroller-tray-symbolic.svg" "${ICON_SVG_DIR}/wavecontroller-tray-symbolic.svg"
    fi

    # Create Desktop Launcher
    echo -e "${BLUE}[5/5] Creating desktop menu launcher in ${DESKTOP_DIR}...${NC}"
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
StartupWMClass=wavecontroller
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

    echo ""
    echo -e "${GREEN}${BOLD}✔ WaveController installed successfully!${NC}"
    echo -e "Launch it from your application menu or run: ${CYAN}wavecontroller${NC}"
    echo ""
}

# Upgrade Installation
upgrade_app() {
    print_banner
    echo -e "${BLUE}Upgrading WaveController to latest version...${NC}"
    
    # If inside git repo, pull latest
    if [ -d "${REPO_DIR}/.git" ]; then
        echo -e "${CYAN}Pulling latest changes from git repository...${NC}"
        cd "${REPO_DIR}"
        git pull origin main
    fi

    install_core_files
    install_udev_rules
    echo -e "${GREEN}${BOLD}✔ Upgrade complete! Saved configurations were preserved.${NC}"
}

# Autostart Management
enable_autostart() {
    mkdir -p "${AUTOSTART_DIR}"
    cat << EOF > "${AUTOSTART_DIR}/com.oparada.WaveController.desktop"
[Desktop Entry]
Type=Application
Name=WaveController
GenericName=Audio Mixer
Comment=Elgato Wave Link & Advanced Multi-Track Virtual Mixer for Linux
Exec=${BIN_DIR}/wavecontroller --daemon
Icon=com.oparada.WaveController
Terminal=false
Categories=AudioVideo;Audio;Mixer;GTK;
StartupWMClass=com.oparada.WaveController
X-GNOME-Autostart-enabled=true
EOF
    chmod +x "${AUTOSTART_DIR}/com.oparada.WaveController.desktop"
    echo -e "${GREEN}✔ WaveController background auto-start enabled in ~/.config/autostart.${NC}"
}

disable_autostart() {
    rm -f "${AUTOSTART_DIR}/com.oparada.WaveController.desktop"
    systemctl --user stop wavecontroller.service 2>/dev/null || true
    systemctl --user disable wavecontroller.service 2>/dev/null || true
    rm -f "${SYSTEMD_USER_DIR}/wavecontroller.service" 2>/dev/null || true
    echo -e "${YELLOW}✔ WaveController background auto-start disabled.${NC}"
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

    echo -e "${GREEN}✔ Application, desktop entry, and icons removed.${NC}"
    
    # Prompt for configuration removal
    if [ -d "${CONFIG_DIR}" ]; then
        read -p "Do you also want to remove your saved settings (~/.config/WaveController)? [y/N]: " confirm
        if [[ "$confirm" =~ ^[Yy]$ ]]; then
            rm -rf "${CONFIG_DIR}"
            echo -e "${GREEN}✔ User configuration purged.${NC}"
        else
            echo -e "${CYAN}Configuration files preserved at ${CONFIG_DIR}.${NC}"
        fi
    fi
    echo ""
    echo -e "${GREEN}${BOLD}✔ Uninstallation complete.${NC}"
}

# Main Execution Routing
case "$1" in
    --install|-i|"")
        print_banner
        install_udev_rules
        install_core_files
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
