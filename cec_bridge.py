#!/usr/bin/env python3
"""
CEC-Sonos Bridge
Monitors HDMI-CEC for TV remote volume commands and controls Sonos speaker.
Also runs a web server for admin access at http://sonosbridge.local

Uses cec-client which talks to the Pi's VideoCore GPU for hardware CEC support.

Volume commands from TV:
  05:44:41 = Volume Up
  05:44:42 = Volume Down
  05:44:43 = Mute Toggle

Hardware: Raspberry Pi Zero 2 W connected to TV via HDMI
"""

import subprocess
import json
import os
import sys
import time
import signal
import logging
from threading import Thread

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

# Configuration
APP_DIR = '/opt/cec-sonos-bridge'
CONFIG_FILE = f'{APP_DIR}/config.json'

# WiFi monitoring
WIFI_CHECK_INTERVAL = 60  # Check WiFi every 60 seconds
WIFI_FAIL_THRESHOLD = 3   # Reboot after 3 consecutive failures


def load_config():
    """Load speaker configuration."""
    if not os.path.exists(CONFIG_FILE):
        log.error("No config found!")
        log.error("Run the setup wizard first")
        return None
    
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Error loading config: {e}")
        return None


def is_wifi_connected():
    """Check if WiFi is connected."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'DEVICE,STATE', 'device', 'status'],
            capture_output=True, text=True, timeout=10
        )
        return 'wlan0:connected' in result.stdout
    except:
        return False


def handle_volume(speaker_ip, direction):
    """Change Sonos volume up or down."""
    try:
        import soco
        speaker = soco.SoCo(speaker_ip)
        change = 2 if direction == "up" else -2
        new_vol = max(0, min(100, speaker.volume + change))
        speaker.volume = new_vol
        log.info(f"Volume {direction} -> {new_vol}%")
    except Exception as e:
        log.error(f"Volume error: {e}")


def handle_mute(speaker_ip):
    """Toggle Sonos mute."""
    try:
        import soco
        speaker = soco.SoCo(speaker_ip)
        speaker.mute = not speaker.mute
        state = "muted" if speaker.mute else "unmuted"
        log.info(f"Mute toggled -> {state}")
    except Exception as e:
        log.error(f"Mute error: {e}")


def start_web_server():
    """Start the admin web server in a separate thread."""
    try:
        # Import and run web server
        sys.path.insert(0, APP_DIR)
        from web_server import run_server
        log.info("Starting admin web server...")
        run_server(port=80)
    except Exception as e:
        log.error(f"Web server error: {e}")


def display_splash_screen():
    """Display splash screen on TV."""
    try:
        sys.path.insert(0, APP_DIR)
        from splash_screen import generate_splash_image, display_splash
        log.info("Displaying splash screen on TV...")
        generate_splash_image()
        display_splash()
    except Exception as e:
        log.warning(f"Could not display splash screen: {e}")


def run_bridge(config):
    """Main CEC monitoring loop."""
    speaker_ip = config['speaker_ip']
    speaker_name = config.get('speaker_name', 'Sonos')
    
    log.info("=" * 50)
    log.info("CEC-Sonos Bridge Active")
    log.info(f"Speaker: {speaker_name} ({speaker_ip})")
    log.info(f"Admin panel: http://sonosbridge.local")
    log.info("=" * 50)
    log.info("")
    log.info("Listening for TV remote volume commands...")
    log.info("  Volume Up:   05:44:41")
    log.info("  Volume Down: 05:44:42")
    log.info("  Mute:        05:44:43")
    log.info("")
    
    # Start cec-client as audio system device
    # -t a = audio system type
    # -o = OSD name (what shows on TV, max 12 chars)  
    # -d 8 = debug level (shows all CEC traffic)
    osd_name = speaker_name[:12].replace(' ', '')
    
    proc = subprocess.Popen(
        ["cec-client", "-t", "a", "-o", osd_name, "-d", "8"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    
    log.info("CEC client started")
    
    wifi_fail_count = 0
    last_wifi_check = time.time()
    
    try:
        for line in proc.stdout:
            # Check WiFi periodically
            now = time.time()
            if now - last_wifi_check > WIFI_CHECK_INTERVAL:
                last_wifi_check = now
                if is_wifi_connected():
                    wifi_fail_count = 0
                else:
                    wifi_fail_count += 1
                    log.warning(f"WiFi disconnected (count: {wifi_fail_count})")
                    if wifi_fail_count >= WIFI_FAIL_THRESHOLD:
                        log.error("WiFi lost for too long, rebooting to AP mode...")
                        os.system('reboot')
            
            # Process CEC messages
            # Look for volume commands from TV to Audio System
            # Format: ">> 05:44:41" or ">> 0f:44:41" (broadcast)
            
            if ">> 05:44:41" in line or ">> 0f:44:41" in line:
                handle_volume(speaker_ip, "up")
                
            elif ">> 05:44:42" in line or ">> 0f:44:42" in line:
                handle_volume(speaker_ip, "down")
                
            elif ">> 05:44:43" in line or ">> 0f:44:43" in line:
                handle_mute(speaker_ip)
            
            # Also catch broadcast volume commands
            elif ":44:41" in line and ">>" in line:
                handle_volume(speaker_ip, "up")
            elif ":44:42" in line and ">>" in line:
                handle_volume(speaker_ip, "down")
            elif ":44:43" in line and ">>" in line:
                handle_mute(speaker_ip)
                
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except:
            proc.kill()


def main():
    """Main entry point."""
    log.info("CEC-Sonos Bridge starting...")
    
    config = load_config()
    if not config:
        log.error("No configuration found. Exiting.")
        sys.exit(1)
    
    # Check WiFi before starting
    if not is_wifi_connected():
        log.warning("WiFi not connected, waiting...")
        for i in range(30):
            time.sleep(2)
            if is_wifi_connected():
                log.info("WiFi connected!")
                break
        else:
            log.error("WiFi connection failed, rebooting...")
            os.system('reboot')
    
    # Start web server in background thread
    web_thread = Thread(target=start_web_server, daemon=True)
    web_thread.start()
    
    # Display splash screen on TV
    display_splash_screen()
    
    # Run CEC bridge
    run_bridge(config)


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            log.exception(f"Bridge error: {e}")
            log.info("Restarting in 10 seconds...")
            time.sleep(10)
