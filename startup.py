#!/usr/bin/env python3
"""
CEC-Sonos Bridge - Startup Manager
Decides which mode to run at boot:
  1. AP Mode - If no WiFi configured or can't connect
  2. Bridge Mode - If WiFi connected and Sonos configured

Supports debug flags on boot partition:
  /boot/firmware/FORCE_AP_MODE - Forces AP mode even if configured
  /boot/firmware/SKIP_AP_MODE - Skips AP mode, goes straight to bridge

Hardware: Raspberry Pi Zero 2 W running Bookworm
"""

import os
import sys
import time
import subprocess
import json
import logging

# Setup logging
LOG_FILE = '/var/log/cec-sonos-bridge.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Paths
APP_DIR = '/opt/cec-sonos-bridge'
CONFIG_FILE = f'{APP_DIR}/config.json'
BOOT_DIR = '/boot/firmware'
FORCE_AP_FLAG = f'{BOOT_DIR}/FORCE_AP_MODE'
SKIP_AP_FLAG = f'{BOOT_DIR}/SKIP_AP_MODE'

# Timeouts
WIFI_CONNECT_TIMEOUT = 30  # seconds to wait for WiFi
WIFI_RETRY_COUNT = 3


def run_cmd(cmd, timeout=30):
    """Run a shell command and return (success, output)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, 
            text=True, timeout=timeout
        )
        return result.returncode == 0, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def check_force_ap_mode():
    """Check if FORCE_AP_MODE flag exists."""
    if os.path.exists(FORCE_AP_FLAG):
        log.info(f"Found {FORCE_AP_FLAG} - forcing AP mode")
        return True
    return False


def check_skip_ap_mode():
    """Check if SKIP_AP_MODE flag exists."""
    if os.path.exists(SKIP_AP_FLAG):
        log.info(f"Found {SKIP_AP_FLAG} - skipping AP mode")
        return True
    return False


def is_wifi_configured():
    """Check if any WiFi connection is configured (excluding hotspot)."""
    success, output = run_cmd(
        "nmcli -t -f NAME,TYPE connection show | grep ':802-11-wireless$' | grep -v 'SonosBridge'"
    )
    configured = bool(output.strip())
    log.info(f"WiFi configured: {configured}")
    return configured


def is_wifi_connected():
    """Check if WiFi is currently connected."""
    success, output = run_cmd(
        "nmcli -t -f DEVICE,STATE device status | grep '^wlan0:'"
    )
    connected = 'connected' in output.lower() and 'disconnected' not in output.lower()
    log.info(f"WiFi connected: {connected}")
    return connected


def get_wifi_ip():
    """Get the current WiFi IP address."""
    success, output = run_cmd(
        "nmcli -t -f IP4.ADDRESS device show wlan0 | head -1 | cut -d: -f2"
    )
    if success and output:
        return output.split('/')[0]
    return None


def wait_for_wifi(timeout=WIFI_CONNECT_TIMEOUT):
    """Wait for WiFi to connect."""
    log.info(f"Waiting up to {timeout}s for WiFi connection...")
    
    for i in range(timeout):
        if is_wifi_connected():
            ip = get_wifi_ip()
            log.info(f"WiFi connected! IP: {ip}")
            return True
        time.sleep(1)
    
    log.warning("WiFi connection timeout")
    return False


def try_connect_wifi():
    """Try to connect to configured WiFi networks."""
    # Get list of configured WiFi connections
    success, output = run_cmd(
        "nmcli -t -f NAME,TYPE connection show | grep ':802-11-wireless$' | grep -v 'SonosBridge' | cut -d: -f1"
    )
    
    if not output:
        log.info("No WiFi connections configured")
        return False
    
    connections = [c.strip() for c in output.split('\n') if c.strip()]
    
    for conn in connections:
        log.info(f"Trying to connect to: {conn}")
        for attempt in range(WIFI_RETRY_COUNT):
            success, _ = run_cmd(f'nmcli connection up "{conn}"', timeout=30)
            if success and wait_for_wifi(timeout=15):
                return True
            log.info(f"Attempt {attempt + 1} failed for {conn}")
            time.sleep(2)
    
    return False


def is_sonos_configured():
    """Check if Sonos speaker has been configured."""
    if not os.path.exists(CONFIG_FILE):
        return False
    
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        configured = bool(config.get('speaker_ip'))
        log.info(f"Sonos configured: {configured}")
        return configured
    except Exception as e:
        log.error(f"Error reading config: {e}")
        return False


def start_ap_mode():
    """Start AP mode with setup wizard."""
    log.info("=" * 50)
    log.info("STARTING AP MODE")
    log.info("Connect to WiFi: SonosBridge-Setup")
    log.info("Password: sonosbridge")
    log.info("Then open: http://192.168.4.1")
    log.info("=" * 50)
    
    os.execv(sys.executable, [sys.executable, f'{APP_DIR}/ap_mode.py'])


def start_bridge_mode():
    """Start the CEC-Sonos bridge."""
    log.info("=" * 50)
    log.info("STARTING BRIDGE MODE")
    log.info("TV remote now controls Sonos!")
    log.info("=" * 50)
    
    os.execv(sys.executable, [sys.executable, f'{APP_DIR}/cec_bridge.py'])


def main():
    """Main startup logic."""
    log.info("=" * 50)
    log.info("CEC-Sonos Bridge - Startup")
    log.info("=" * 50)
    
    # Ensure app directory exists
    os.makedirs(APP_DIR, exist_ok=True)
    
    # Check for debug flags
    if check_force_ap_mode():
        start_ap_mode()
        return
    
    if check_skip_ap_mode():
        log.info("Skipping AP mode check, going to bridge")
        if is_sonos_configured():
            start_bridge_mode()
        else:
            log.error("Sonos not configured but SKIP_AP_MODE set!")
            log.error("Remove SKIP_AP_MODE flag and reboot")
            sys.exit(1)
        return
    
    # Normal boot flow
    log.info("Checking WiFi configuration...")
    
    if not is_wifi_configured():
        log.info("No WiFi configured -> AP Mode")
        start_ap_mode()
        return
    
    log.info("WiFi is configured, attempting connection...")
    
    # Wait a moment for NetworkManager to initialize
    time.sleep(5)
    
    if not is_wifi_connected():
        if not try_connect_wifi():
            log.info("Cannot connect to WiFi -> AP Mode")
            start_ap_mode()
            return
    
    log.info("WiFi connected!")
    ip = get_wifi_ip()
    log.info(f"IP Address: {ip}")
    
    # Check Sonos configuration
    if not is_sonos_configured():
        log.info("Sonos not configured -> AP Mode")
        start_ap_mode()
        return
    
    log.info("All configured -> Bridge Mode")
    start_bridge_mode()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log.exception(f"Startup failed: {e}")
        log.info("Falling back to AP mode")
        start_ap_mode()
