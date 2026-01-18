#!/bin/bash
# ============================================================
# CEC-Sonos Bridge - Installer v1.2.0
# ============================================================

set -e
VERSION="1.2.0"

echo ""
echo "=================================================="
echo "  CEC-Sonos Bridge Installer v${VERSION}"
echo "=================================================="
echo ""

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please run as root (sudo bash install.sh)"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/cec-sonos-bridge"

echo "📦 [1/7] Updating system..."
apt-get update -qq

echo "📦 [2/7] Installing dependencies..."
apt-get install -y -qq python3-pip cec-utils fbi fonts-dejavu avahi-daemon network-manager
pip3 install --break-system-packages soco qrcode pillow 2>/dev/null || pip3 install soco qrcode pillow

echo "📁 [3/7] Installing application files..."
mkdir -p "$APP_DIR/backups"
cp "$SCRIPT_DIR"/*.py "$APP_DIR/"
cp "$SCRIPT_DIR"/version.json "$APP_DIR/"
chmod +x "$APP_DIR"/*.py

echo "⚙️  [4/7] Creating system service..."
cat > /etc/systemd/system/cec-sonos-bridge.service << 'SERVICE'
[Unit]
Description=CEC-Sonos Bridge
After=network.target NetworkManager.service
Wants=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/cec-sonos-bridge
ExecStart=/usr/bin/python3 /opt/cec-sonos-bridge/startup.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/cec-sonos-bridge.log
StandardError=append:/var/log/cec-sonos-bridge.log
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable cec-sonos-bridge.service

echo "🎨 [5/7] Configuring boot settings..."
CMDLINE="/boot/firmware/cmdline.txt"
CONFIG="/boot/firmware/config.txt"

if ! grep -q "quiet" "$CMDLINE" 2>/dev/null; then
    CURRENT=$(cat "$CMDLINE" | tr '\n' ' ')
    echo "$CURRENT quiet splash loglevel=0 logo.nologo vt.global_cursor_default=0" > "$CMDLINE"
fi

if ! grep -q "disable_splash=1" "$CONFIG" 2>/dev/null; then
    echo -e "\n# CEC-Sonos Bridge\ndisable_splash=1" >> "$CONFIG"
fi

echo "🌐 [6/7] Configuring network..."
hostnamectl set-hostname sonosbridge 2>/dev/null || echo "sonosbridge" > /etc/hostname
grep -q "sonosbridge" /etc/hosts || echo "127.0.1.1 sonosbridge" >> /etc/hosts

mkdir -p /etc/avahi/services
cat > /etc/avahi/services/sonos-bridge.service << 'AVAHI'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <n>Sonos Bridge</n>
  <service><type>_http._tcp</type><port>80</port></service>
</service-group>
AVAHI
systemctl enable avahi-daemon 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true

echo "📺 [7/7] Generating splash screen..."
touch /var/log/cec-sonos-bridge.log
chmod 666 /var/log/cec-sonos-bridge.log
python3 "$APP_DIR/splash_screen.py" generate 2>/dev/null || echo "   (will generate on first boot)"

echo ""
echo "=================================================="
echo "  ✓ Installation Complete!"
echo "=================================================="
echo ""
echo "FIRST BOOT:"
echo "  1. Pi creates hotspot: SonosBridge-Setup"
echo "  2. Connect phone to hotspot (password: sonosbridge)"
echo "  3. Open http://sonosbridge.local"
echo "  4. Select WiFi and Sonos speaker"
echo "  5. Done! TV remote controls Sonos"
echo ""
echo "ADMIN PANEL: http://sonosbridge.local"
echo "  - Test volume, update, rollback, logs"
echo ""
echo "DEBUG: Create /boot/firmware/FORCE_AP_MODE to reset"
echo ""

read -p "Reboot now? (y/n) " -n 1 -r
echo ""
[[ $REPLY =~ ^[Yy]$ ]] && reboot || echo "Run 'sudo reboot' when ready"
