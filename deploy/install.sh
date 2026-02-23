#!/usr/bin/env bash
# Install Metroplex as a systemd user service.
set -euo pipefail

SERVICE_NAME="metroplex.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SRC="${SCRIPT_DIR}/${SERVICE_NAME}"
SERVICE_DST="${HOME}/.config/systemd/user/${SERVICE_NAME}"

# Ensure user systemd directory exists
mkdir -p "${HOME}/.config/systemd/user"

# Copy service file
cp "${SERVICE_SRC}" "${SERVICE_DST}"
echo "Installed ${SERVICE_DST}"

# Reload systemd
systemctl --user daemon-reload
echo "Reloaded systemd user daemon"

echo ""
echo "Usage:"
echo "  systemctl --user start metroplex       # Start service"
echo "  systemctl --user stop metroplex        # Stop service (SIGTERM)"
echo "  systemctl --user status metroplex      # Check status"
echo "  systemctl --user enable metroplex      # Start on login"
echo "  journalctl --user -u metroplex -f      # Follow logs"
