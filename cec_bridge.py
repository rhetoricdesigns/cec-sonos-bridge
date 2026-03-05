#!/usr/bin/env python3
"""
CEC-Sonos Bridge v1.5.0
Monitors HDMI-CEC for TV remote volume commands and controls Sonos speaker.
Also runs a web server for admin access at http://sonosbridge.local

Uses cec-client which talks to the Pi's VideoCore GPU for hardware CEC support.

Key improvements over v1.4.0:
  - Auto TV brand detection via CEC Vendor ID (opcode 0x87)
  - Samsung (Anynet+): ARC declined — Samsung drops connection if accepted
  - LG (SimpLink):     ARC accepted — LG won't recognise audio system otherwise
  - LG reconnect handling: when LG periodically terminates ARC (normal behaviour),
    we acknowledge, re-assert System Audio Mode, and re-accept the next initiation
  - Handles LG's non-standard 0x8B Vendor Remote Button Up (key release)
  - Works with any TV brand; falls back to "accept ARC" for unknowns

TV brand detection:
  CEC opcode 0x87 (Device Vendor ID) is broadcast by the TV on startup.
  Samsung vendor ID: 00:00:F0
  LG vendor ID:      00:E0:91
  Sony vendor ID:    00:08:00  (treated as accept-ARC)

CEC Opcodes handled:
  Incoming:
    44:41 = Volume Up    44:42 = Volume Down    44:43 = Mute
    45    = Key Released
    70    = System Audio Mode Request
    71    = Give Audio Status
    7D    = Give System Audio Mode Status
    87    = Device Vendor ID  (used to detect TV brand)
    8B    = LG Vendor Remote Button Up (key release, ignored)
    C0    = Request ARC Initiation (accepted for LG; declined for Samsung)
    C3    = Request ARC Initiation alt (accepted for LG; declined for Samsung)
    C4    = Request ARC Termination (acknowledged; re-assert SAM for LG)
    A4    = Request Short Audio Descriptor (declined)

  Outgoing:
    72:01 = Set System Audio Mode ON
    7A:xx = Report Audio Status
    7E:01 = System Audio Mode Status ON
    C1    = Report ARC Initiated (LG only)
    C2    = Report ARC Terminated (LG only)
    00    = Feature Abort (Samsung ARC decline)

Hardware: Raspberry Pi Zero 2 W
  Samsung: use any non-ARC HDMI port
  LG:      use the ARC-labelled HDMI port (usually HDMI 2)
"""

import subprocess
import json
import os
import sys
import time
import re
import signal
import logging
from threading import Thread, Lock

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
WIFI_CHECK_INTERVAL = 60
WIFI_FAIL_THRESHOLD = 3

# CEC System Audio Mode - smart keepalive
# Only reasserts when volume commands stop flowing
SAM_CHECK_INTERVAL = 120   # Check every 2 minutes
SAM_IDLE_THRESHOLD = 120   # Reassert after 2 minutes of silence
SAM_STARTUP_DELAY = 5      # Wait for cec-client to initialize

# Volume tracking
current_volume = 30
is_muted = False
volume_lock = Lock()

# CEC process handle
cec_proc = None
cec_lock = Lock()

# Smart keepalive tracking
last_volume_command_time = 0
last_volume_lock = Lock()

# TV brand detection (set from CEC Vendor ID opcode 0x87)
# Controls ARC accept/decline behaviour
tv_brand = 'unknown'   # 'samsung', 'lg', or 'unknown'
tv_brand_lock = Lock()

# Known CEC vendor IDs (3-byte, uppercase hex joined by colons)
TV_VENDORS = {
    '00:00:F0': 'samsung',
    '00:E0:91': 'lg',
    '00:08:00': 'sony',
    '00:90:F5': 'philips',
    '00:00:39': 'toshiba',
}


def load_config():
    """Load speaker configuration."""
    if not os.path.exists(CONFIG_FILE):
        log.error("No config found! Run the setup wizard first")
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


def sync_volume_from_sonos(speaker_ip):
    """Read current volume from Sonos to stay in sync."""
    global current_volume, is_muted
    try:
        import soco
        speaker = soco.SoCo(speaker_ip)
        with volume_lock:
            current_volume = speaker.volume
            is_muted = speaker.mute
        log.info(f"Synced volume from Sonos: {current_volume}%, muted={is_muted}")
    except Exception as e:
        log.warning(f"Could not sync volume from Sonos: {e}")


def handle_volume(speaker_ip, direction):
    """Change Sonos volume up or down."""
    global current_volume, is_muted, last_volume_command_time
    try:
        import soco
        speaker = soco.SoCo(speaker_ip)
        change = 2 if direction == "up" else -2
        new_vol = max(0, min(100, speaker.volume + change))
        speaker.volume = new_vol
        with volume_lock:
            current_volume = new_vol
            is_muted = False
        with last_volume_lock:
            last_volume_command_time = time.time()
        log.info(f"Volume {direction} -> {new_vol}%")
        report_audio_status()
    except Exception as e:
        log.error(f"Volume error: {e}")


def handle_mute(speaker_ip):
    """Toggle Sonos mute."""
    global is_muted, last_volume_command_time
    try:
        import soco
        speaker = soco.SoCo(speaker_ip)
        speaker.mute = not speaker.mute
        with volume_lock:
            is_muted = speaker.mute
        with last_volume_lock:
            last_volume_command_time = time.time()
        state = "muted" if speaker.mute else "unmuted"
        log.info(f"Mute toggled -> {state}")
        report_audio_status()
    except Exception as e:
        log.error(f"Mute error: {e}")


def send_cec_command(command):
    """Send a CEC command via cec-client stdin."""
    global cec_proc
    with cec_lock:
        if cec_proc and cec_proc.stdin:
            try:
                cec_proc.stdin.write(command + "\n")
                cec_proc.stdin.flush()
                log.info(f"CEC TX: {command}")
            except Exception as e:
                log.error(f"Failed to send CEC command '{command}': {e}")


def report_audio_status():
    """Report current volume/mute to TV (opcode 0x7A).
    Bit 7 = mute, Bits 6-0 = volume percentage.
    """
    with volume_lock:
        vol = current_volume & 0x7F
        if is_muted:
            vol |= 0x80
    send_cec_command(f"tx 50:7A:{vol:02X}")


def assert_system_audio_mode():
    """Broadcast System Audio Mode ON so TV routes volume to us."""
    send_cec_command("tx 5F:72:01")


def is_addressed_to_audio_system(line):
    """Check if message is addressed to logical address 5 or broadcast F."""
    match = re.search(r'>>\s*([0-9a-fA-F])([0-9a-fA-F]):', line)
    if match:
        dest = match.group(2).upper()
        return dest in ('5', 'F')
    return False


def detect_tv_brand(line):
    """Parse CEC opcode 0x87 (Device Vendor ID) from the TV (LA 0).

    Called from the main loop on every CEC line.  When the TV broadcasts
    its vendor ID we store the brand so ARC handling can adapt.

    Format on the bus:  >> 0F:87:VV:VV:VV
      Source 0 (TV), destination F (broadcast), opcode 87, 3 vendor bytes.
    """
    global tv_brand
    # Only care about messages FROM address 0 (TV) with opcode 87
    match = re.search(r'>>\s*0[Ff]:87:([0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})', line)
    if not match:
        return
    vendor_id = match.group(1).upper()
    with tv_brand_lock:
        detected = TV_VENDORS.get(vendor_id, 'unknown')
        if tv_brand != detected:
            tv_brand = detected
            log.info(f"TV brand detected: {tv_brand} (vendor ID {vendor_id})")


def arc_accept_mode():
    """Return True if this TV brand requires ARC to be accepted."""
    with tv_brand_lock:
        brand = tv_brand
    # Samsung (Anynet+) must have ARC declined.
    # Everything else (LG, Sony, unknown) gets ARC accepted.
    return brand != 'samsung'


def handle_cec_handshake(line):
    """Respond to CEC handshake messages — brand-aware ARC handling.

    Samsung (Anynet+): ARC must be DECLINED or Samsung drops us.
    LG (SimpLink):     ARC must be ACCEPTED or LG never recognises the Pi
                       as an audio system, and Apple TV never offers 'HDMI'
                       volume mode.

    LG periodically terminates and re-initiates ARC (every few minutes) —
    this is normal.  We acknowledge the termination, then re-assert System
    Audio Mode so we stay recognised between renegotiations.

    Returns True if we handled the message.
    """
    if not is_addressed_to_audio_system(line):
        return False

    match = re.search(r'>>\s*[0-9a-fA-F]{2}:(.+)', line)
    if not match:
        return False

    data = match.group(1).strip().upper()

    # 0x70 - System Audio Mode Request -> respond ON + report volume
    if data.startswith('70'):
        log.info("CEC RX: System Audio Mode Request -> ON")
        send_cec_command("tx 5F:72:01")
        report_audio_status()
        return True

    # 0x71 - Give Audio Status -> report volume
    if data == '71':
        log.info("CEC RX: Give Audio Status -> reporting")
        report_audio_status()
        return True

    # 0x7D - Give System Audio Mode Status -> ON
    if data == '7D':
        log.info("CEC RX: Give System Audio Mode Status -> ON")
        send_cec_command("tx 50:7E:01")
        return True

    # 0x8B - LG Vendor Remote Button Up (non-standard key release)
    # LG sends this instead of the standard 0x45 User Control Released.
    if data.startswith('8B'):
        log.debug("CEC RX: LG Vendor Remote Button Up (0x8B) - ignored")
        return True

    # 0xC0 / 0xC3 - Request ARC Initiation
    if data in ('C0', 'C3'):
        if arc_accept_mode():
            log.info(f"CEC RX: ARC Initiation (0x{data}) -> ACCEPTED (LG/unknown mode)")
            send_cec_command("tx 50:C1")    # Report ARC Initiated
        else:
            log.info(f"CEC RX: ARC Initiation (0x{data}) -> DECLINED (Samsung mode)")
            send_cec_command(f"tx 50:00:{data}:00")  # Feature Abort
        return True

    # 0xC4 - Request ARC Termination
    if data == 'C4':
        if arc_accept_mode():
            # LG periodically terminates ARC then re-initiates — this is normal.
            # Acknowledge, then immediately re-assert System Audio Mode so we
            # stay visible as an audio system during the brief gap.
            log.info("CEC RX: ARC Termination -> acknowledged (LG renegotiation)")
            send_cec_command("tx 50:C2")    # Report ARC Terminated
            # Re-assert SAM after a short pause so LG re-discovers us
            def _reassert():
                time.sleep(1)
                send_cec_command("tx 5F:72:01")
                report_audio_status()
            Thread(target=_reassert, daemon=True).start()
        else:
            log.info("CEC RX: ARC Termination -> declined (Samsung mode, no-op)")
            send_cec_command("tx 50:00:C4:00")  # Feature Abort
        return True

    # 0xA4 - Request Short Audio Descriptor -> decline (not supported)
    if data.startswith('A4'):
        log.info("CEC RX: Request Short Audio Descriptor -> Feature Abort")
        send_cec_command("tx 50:00:A4:00")
        return True

    return False


def system_audio_keepalive():
    """Smart keepalive - only reasserts System Audio Mode when idle.

    If volume commands are flowing, the TV knows about us.
    If they stop for 2+ minutes, the TV may have forgotten us,
    so we send a single reminder.
    """
    log.info(f"Smart keepalive started (reassert after {SAM_IDLE_THRESHOLD}s idle)")

    time.sleep(SAM_STARTUP_DELAY)

    # Always assert once on startup
    log.info("Initial System Audio Mode assertion")
    assert_system_audio_mode()

    while True:
        time.sleep(SAM_CHECK_INTERVAL)
        try:
            with last_volume_lock:
                idle_time = time.time() - last_volume_command_time

            if idle_time > SAM_IDLE_THRESHOLD:
                log.info(f"Idle {idle_time:.0f}s -> reasserting System Audio Mode")
                assert_system_audio_mode()
        except Exception as e:
            log.warning(f"Keepalive error: {e}")


def start_web_server():
    """Start the admin web server in a separate thread."""
    try:
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
    global cec_proc

    speaker_ip = config['speaker_ip']
    speaker_name = config.get('speaker_name', 'Sonos')
    hdmi_port = config.get('hdmi_port', '2')

    log.info("=" * 50)
    log.info("CEC-Sonos Bridge v1.5.0 Active")
    log.info(f"Speaker: {speaker_name} ({speaker_ip})")
    log.info(f"HDMI Port: {hdmi_port}")
    log.info(f"Admin: http://sonosbridge.local")
    log.info("=" * 50)
    log.info("")
    log.info("Volume commands: :44:41 (up) :44:42 (down) :44:43 (mute)")
    log.info("Smart keepalive: reassert after %ds idle", SAM_IDLE_THRESHOLD)
    log.info("ARC: auto (LG=accept, Samsung=decline, detected from vendor ID)")
    log.info("")

    sync_volume_from_sonos(speaker_ip)

    osd_name = speaker_name[:12].replace(' ', '')

    cec_proc = subprocess.Popen(
        ["cec-client", "-t", "a", "-o", osd_name, "-d", "8"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    log.info("CEC client started with stdin enabled")

    # Start background threads
    Thread(target=system_audio_keepalive, daemon=True).start()

    def volume_sync_loop():
        while True:
            time.sleep(300)
            sync_volume_from_sonos(speaker_ip)

    Thread(target=volume_sync_loop, daemon=True).start()

    wifi_fail_count = 0
    last_wifi_check = time.time()
    last_vol_time = 0
    VOL_DEBOUNCE = 0.05

    try:
        for line in cec_proc.stdout:
            line = line.strip()
            if not line:
                continue

            # WiFi check
            now = time.time()
            if now - last_wifi_check > WIFI_CHECK_INTERVAL:
                last_wifi_check = now
                if is_wifi_connected():
                    wifi_fail_count = 0
                else:
                    wifi_fail_count += 1
                    log.warning(f"WiFi disconnected (count: {wifi_fail_count})")
                    if wifi_fail_count >= WIFI_FAIL_THRESHOLD:
                        log.error("WiFi lost too long, rebooting...")
                        os.system('reboot')

            # Only process incoming CEC traffic
            if ">>" not in line:
                continue

            # Detect TV brand from vendor ID broadcast (opcode 0x87)
            detect_tv_brand(line)

            # Handle handshake messages first
            if handle_cec_handshake(line):
                continue

            # Volume commands from ANY source device
            now = time.time()

            if ":44:41" in line:
                if now - last_vol_time > VOL_DEBOUNCE:
                    last_vol_time = now
                    handle_volume(speaker_ip, "up")

            elif ":44:42" in line:
                if now - last_vol_time > VOL_DEBOUNCE:
                    last_vol_time = now
                    handle_volume(speaker_ip, "down")

            elif ":44:43" in line:
                if now - last_vol_time > VOL_DEBOUNCE:
                    last_vol_time = now
                    handle_mute(speaker_ip)

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        if cec_proc:
            cec_proc.terminate()
            try:
                cec_proc.wait(timeout=5)
            except:
                cec_proc.kill()


def main():
    """Main entry point."""
    log.info("CEC-Sonos Bridge v1.5.0 starting...")

    config = load_config()
    if not config:
        log.error("No configuration found. Exiting.")
        sys.exit(1)

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

    Thread(target=start_web_server, daemon=True).start()
    display_splash_screen()
    run_bridge(config)


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            log.exception(f"Bridge error: {e}")
            log.info("Restarting in 10 seconds...")
            time.sleep(10)
