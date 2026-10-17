#!/usr/bin/env bash
# ==============================================================================
# WaveController Package Builder (.deb and .rpm)
# ==============================================================================
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${REPO_DIR}/dist"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Extract version from wavecontroller/__init__.py
VERSION=$(python3 -c "import re; f=open('${REPO_DIR}/wavecontroller/__init__.py').read(); print(re.search(r'__version__\s*=\s*[\"\']([^\"\']+)[\"\']', f).group(1))" 2>/dev/null || echo "0.0.4.1")

echo -e "${CYAN}${BOLD}"
echo "=================================================================="
echo "          WaveController System Package Builder                   "
echo "          Version: ${VERSION}                                     "
echo "=================================================================="
echo -e "${NC}"

BUILD_DEB=false
BUILD_RPM=false

if [ "$#" -eq 0 ]; then
    BUILD_DEB=true
    if command -v rpmbuild &>/dev/null; then
        BUILD_RPM=true
    fi
else
    for arg in "$@"; do
        case "$arg" in
            --deb) BUILD_DEB=true ;;
            --rpm) BUILD_RPM=true ;;
            --all) BUILD_DEB=true; BUILD_RPM=true ;;
            *)
                echo -e "${RED}Unknown argument: $arg${NC}"
                echo "Usage: ./scripts/build_packages.sh [--deb] [--rpm] [--all]"
                exit 1
                ;;
        esac
    done
fi

mkdir -p "${DIST_DIR}"

# ------------------------------------------------------------------------------
# Build Debian (.deb) Package
# ------------------------------------------------------------------------------
if [ "${BUILD_DEB}" = true ]; then
    echo -e "${BLUE}>>> Building Debian package (.deb)...${NC}"
    if ! command -v dpkg-deb &>/dev/null; then
        echo -e "${RED}[ERROR] dpkg-deb not found. Please install dpkg (sudo apt install dpkg).${NC}"
        exit 1
    fi

    PKG_NAME="wavecontroller_${VERSION}_all"
    STAGE_DIR="${REPO_DIR}/build/deb/${PKG_NAME}"
    rm -rf "${STAGE_DIR}"
    mkdir -p "${STAGE_DIR}"

    # Standard FHS tree
    mkdir -p "${STAGE_DIR}/DEBIAN"
    mkdir -p "${STAGE_DIR}/usr/bin"
    mkdir -p "${STAGE_DIR}/usr/share/wavecontroller"
    mkdir -p "${STAGE_DIR}/usr/share/applications"
    mkdir -p "${STAGE_DIR}/usr/share/icons/hicolor/512x512/apps"
    mkdir -p "${STAGE_DIR}/usr/share/icons/hicolor/scalable/apps"
    mkdir -p "${STAGE_DIR}/usr/share/icons/hicolor/symbolic/apps"
    mkdir -p "${STAGE_DIR}/usr/lib/udev/rules.d"
    mkdir -p "${STAGE_DIR}/usr/lib/systemd/user"
    mkdir -p "${STAGE_DIR}/usr/share/wireplumber/wireplumber.conf.d"
    mkdir -p "${STAGE_DIR}/usr/lib/ladspa"

    # Control files
    sed "s/^Version: .*/Version: ${VERSION}/" "${REPO_DIR}/packaging/debian/control" > "${STAGE_DIR}/DEBIAN/control"
    cp "${REPO_DIR}/packaging/debian/postinst" "${STAGE_DIR}/DEBIAN/postinst"
    cp "${REPO_DIR}/packaging/debian/postrm" "${STAGE_DIR}/DEBIAN/postrm"
    chmod 0755 "${STAGE_DIR}/DEBIAN/postinst" "${STAGE_DIR}/DEBIAN/postrm"

    # Application Files
    rsync -a --exclude '__pycache__' --exclude '*.pyc' \
        "${REPO_DIR}/main.py" \
        "${REPO_DIR}/requirements.txt" \
        "${REPO_DIR}/wavecontroller" \
        "${REPO_DIR}/assets" \
        "${REPO_DIR}/data" \
        "${STAGE_DIR}/usr/share/wavecontroller/"

    # Executable Launcher Wrapper
    cat << 'EOF' > "${STAGE_DIR}/usr/bin/wavecontroller"
#!/bin/sh
exec /usr/bin/python3 /usr/share/wavecontroller/main.py "$@"
EOF
    chmod 0755 "${STAGE_DIR}/usr/bin/wavecontroller"

    # Hardware Init Helper
    install -m 0755 "${REPO_DIR}/scripts/wavecontroller_hw_init.py" "${STAGE_DIR}/usr/bin/wavecontroller-hw-init"

    # Desktop Launcher
    install -m 0644 "${REPO_DIR}/packaging/common/com.oparada.WaveController.desktop" "${STAGE_DIR}/usr/share/applications/"

    # Icons
    if [ -f "${REPO_DIR}/assets/icons/com.oparada.WaveController.png" ]; then
        install -m 0644 "${REPO_DIR}/assets/icons/com.oparada.WaveController.png" "${STAGE_DIR}/usr/share/icons/hicolor/512x512/apps/com.oparada.WaveController.png"
    fi
    for f in "${REPO_DIR}"/assets/icons/*-symbolic.svg; do
        [ -f "$f" ] && install -m 0644 "$f" "${STAGE_DIR}/usr/share/icons/hicolor/symbolic/apps/"
    done
    for f in "${REPO_DIR}"/assets/icons/*.svg; do
        [ -f "$f" ] && install -m 0644 "$f" "${STAGE_DIR}/usr/share/icons/hicolor/scalable/apps/"
    done

    # Udev Rules
    install -m 0644 "${REPO_DIR}/packaging/common/99-elgato-wave.rules" "${STAGE_DIR}/usr/lib/udev/rules.d/99-elgato-wave.rules"

    # Systemd User Unit
    install -m 0644 "${REPO_DIR}/packaging/common/wavecontroller.service" "${STAGE_DIR}/usr/lib/systemd/user/wavecontroller.service"

    # WirePlumber Configuration
    install -m 0644 "${REPO_DIR}/data/51-wavecontroller-wave-xlr.conf" "${STAGE_DIR}/usr/share/wireplumber/wireplumber.conf.d/51-wavecontroller-wave-xlr.conf"

    # LADSPA Plugins
    if [ -d "${REPO_DIR}/wavecontroller/assets/plugins" ]; then
        install -m 0755 "${REPO_DIR}/wavecontroller/assets/plugins"/*.so "${STAGE_DIR}/usr/lib/ladspa/" 2>/dev/null || true
    fi

    # Build package with dpkg-deb
    dpkg-deb --build --root-owner-group "${STAGE_DIR}" "${DIST_DIR}/${PKG_NAME}.deb"
    echo -e "${GREEN}[OK] Debian package created: ${DIST_DIR}/${PKG_NAME}.deb${NC}"
fi

# ------------------------------------------------------------------------------
# Build Red Hat / Fedora (.rpm) Package
# ------------------------------------------------------------------------------
if [ "${BUILD_RPM}" = true ]; then
    echo -e "${BLUE}>>> Building RPM package (.rpm)...${NC}"
    if ! command -v rpmbuild &>/dev/null; then
        echo -e "${RED}[ERROR] rpmbuild not found. Install rpm-build (Fedora: sudo dnf install rpm-build, Ubuntu: sudo apt install rpm).${NC}"
        exit 1
    fi

    RPM_TOPDIR="${REPO_DIR}/build/rpm"
    rm -rf "${RPM_TOPDIR}"
    mkdir -p "${RPM_TOPDIR}"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

    # Prepare tarball
    SOURCE_TAR="${RPM_TOPDIR}/SOURCES/wavecontroller-${VERSION}.tar.gz"
    tar -czf "${SOURCE_TAR}" \
        --transform "s,^\.,wavecontroller-${VERSION}," \
        --exclude '.git' \
        --exclude 'build' \
        --exclude 'dist' \
        --exclude '__pycache__' \
        -C "${REPO_DIR}" .

    # Prepare spec file
    SPEC_FILE="${RPM_TOPDIR}/SPECS/wavecontroller.spec"
    sed "s/^Version: .*/Version: ${VERSION}/" "${REPO_DIR}/packaging/rpm/wavecontroller.spec" > "${SPEC_FILE}"

    # Build RPM
    rpmbuild --define "_topdir ${RPM_TOPDIR}" -bb "${SPEC_FILE}"
    find "${RPM_TOPDIR}/RPMS" -name "*.rpm" -exec cp {} "${DIST_DIR}/" \;
    echo -e "${GREEN}[OK] RPM package created in ${DIST_DIR}/${NC}"
fi

# ------------------------------------------------------------------------------
# Checksums
# ------------------------------------------------------------------------------
cd "${DIST_DIR}"
sha256sum wavecontroller*.* > SHA256SUMS.txt 2>/dev/null || true

echo ""
echo -e "${GREEN}${BOLD}Packaging complete! Artifacts in ${DIST_DIR}:${NC}"
ls -lh "${DIST_DIR}"
