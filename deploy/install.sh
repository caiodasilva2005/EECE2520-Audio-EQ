#!/usr/bin/env bash
# Install Audio-EQ services on a Raspberry Pi.
# Assumes this repo has been cloned to /opt/audio-eq. Run as root.
set -euo pipefail

INSTALL_ROOT=/opt/EECE2520-Audio-EQ

if [[ "${EUID}" -ne 0 ]]; then
    echo "install.sh must be run as root" >&2
    exit 1
fi

if [[ "$(readlink -f "$(dirname "$0")/..")" != "${INSTALL_ROOT}" ]]; then
    echo "This script expects the repo to live at ${INSTALL_ROOT}" >&2
    echo "Current location: $(readlink -f "$(dirname "$0")/..")" >&2
    exit 1
fi

# 1. Ownership
chown -R root:root "${INSTALL_ROOT}"

# 2. Virtualenv + dependencies
if [[ ! -d "${INSTALL_ROOT}/.venv" ]]; then
    python3 -m venv "${INSTALL_ROOT}/.venv"
fi
"${INSTALL_ROOT}/.venv/bin/pip" install --upgrade pip
"${INSTALL_ROOT}/.venv/bin/pip" install -r "${INSTALL_ROOT}/requirements.txt"

# 3. Systemd units
ln -sf "${INSTALL_ROOT}/deploy/systemd/dac-writer.service" /etc/systemd/system/dac-writer.service
ln -sf "${INSTALL_ROOT}/deploy/systemd/frontend.service" /etc/systemd/system/frontend.service

# 4. Enable + start
systemctl daemon-reload
systemctl enable --now dac-writer.service
systemctl enable --now frontend.service

echo
echo "Done installing Audio-EQ services"                      
