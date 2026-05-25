#!/usr/bin/env bash
# Install Audio-EQ services on a Raspberry Pi.
# Assumes this repo has been cloned to /opt/audio-eq. Run as root.
set -euo pipefail

INSTALL_ROOT=/opt/EECE2520-Audio-EQ
SERVICE_USER=audio-eq

if [[ "${EUID}" -ne 0 ]]; then
    echo "install.sh must be run as root" >&2
    exit 1
fi

if [[ "$(readlink -f "$(dirname "$0")/..")" != "${INSTALL_ROOT}" ]]; then
    echo "This script expects the repo to live at ${INSTALL_ROOT}" >&2
    echo "Current location: $(readlink -f "$(dirname "$0")/..")" >&2
    exit 1
fi

# 1. System user
if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

# 2. Ownership
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_ROOT}"

# 3. Virtualenv + dependencies
if [[ ! -d "${INSTALL_ROOT}/.venv" ]]; then
    sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_ROOT}/.venv"
fi
sudo -u "${SERVICE_USER}" "${INSTALL_ROOT}/.venv/bin/pip" install --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_ROOT}/.venv/bin/pip" install -r "${INSTALL_ROOT}/requirements.txt"

# 4. Systemd unit
ln -sf "${INSTALL_ROOT}/deploy/systemd/dac-writer.service" /etc/systemd/system/dac-writer.service

# 5. Udev rule for IIO sysfs access
cp "${INSTALL_ROOT}/deploy/udev/99-audio-eq-iio.rules" /etc/udev/rules.d/99-audio-eq-iio.rules
udevadm control --reload
udevadm trigger

# 6. Enable + start
systemctl daemon-reload
systemctl enable --now dac-writer.service

echo
echo "Done installing Audio-EQ services"                      
