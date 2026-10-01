#!/usr/bin/env python3
"""
CEC-Sonos Bridge v1.6.0
Monitors HDMI-CEC for TV remote volume commands and controls Sonos speaker.
Also runs a web server for admin access at http://sonosbridge.local

Talks to the kernel's CEC device (/dev/cec0) directly, as a pure Audio System.
Falls back to cec-client when /dev/cec0 is missing (legacy firmware CEC).

Key improvements over v1.5.4:
  - LG mode (admin panel: LG TV tab; off by default).  An LG TV gives its
    remote's volume keys over HDMI-CEC only to a sound device on its ARC port,
    and a speaker on the TV's optical output would go silent there.  So with
    LG mode on, the bridge follows the LG TV's own volume and mute over Wi-Fi,
    through the TV's network API (webOS "second screen", a WebSocket on port
    3001 or 3000), and sets the Sonos to the same number.  When the bridge
    connects, the TV takes the Sonos's level, so the Sonos never jumps; HDMI-CEC
    volume keys that reach the bridge are passed on to the TV the same way.
    With LG mode off, nothing changes.

Key improvements over v1.5.3:
  - Hands the screen back.  The TV still switched to the bridge's input about
    9 seconds after the Fire TV took the screen, with the bridge on HDMI-CEC
    throughout and asking for nothing.  The cause turned out to be the Fire
    TV remote: the Fire TV had HDMI 2, the bridge's input, saved as its own
    (Settings > Equipment Control > Manage Equipment), so Home and app
    buttons switched the TV there by infrared, which HDMI-CEC never sees.
    The TV announces the switch, so the bridge answers it with
    <Set Stream Path> for the device the TV came from, which makes that
    device claim the screen again (a Fire TV, like any Android device,
    accepts it from the audio system; it would reject an <Active Source>
    sent on its behalf).  Only within a minute of that device taking the
    screen, and at most 3 times in 10 minutes, so the bridge's own screen
    can still be chosen on purpose.
  - Shows its picture (the splash screen with the admin panel's address)
    only while the TV shows its input because someone chose it; otherwise
    the TV gets no picture from the bridge, so there is nothing to switch to.
    HDMI-CEC, and so volume control, is unaffected.

Key improvements over v1.5.2 (a Samsung TV switched to the bridge's input
about 20 seconds after it woke up, e.g. from Home on the Fire TV remote):
  - Holds the HDMI connection once the bridge has its TV input.  A TV cuts
    its HDMI connections for a moment while it wakes; a Pi Zero-3 checks the
    connection only every 10 seconds, so the bridge dropped off HDMI-CEC and
    came back about 10 seconds after the TV was on - like a device being
    switched on, which a Samsung switches to.
  - Declines the "powered up" vendor command a Samsung sends after waking,
    instead of answering it as Samsung's own soundbars do.  Samsung TVs
    switch to their soundbars' inputs after waking, and the bridge presents
    itself as a Pulse-Eight device, not a Samsung one.

Key improvements over v1.5.1:
  - CEC activity log: every message the bridge sends and receives (including
    the kernel's own replies) and every HDMI connection change, in plain
    English, on the admin panel's CEC Activity page (sonosbridge.local/cec).
    Flags any message that moves the TV to the bridge's input.

Key improvements over v1.5.0:
  - Never takes over the TV input.  cec-client (libCEC) acts like a video
    source: when the Fire TV or the TV sends the Audio System a power-on key
    (e.g. pressing Home on the Fire TV remote), libCEC broadcasts
    <Active Source> for the Pi and the TV switches to the splash screen.
    The kernel device never does that on its own.
  - Answers the housekeeping messages libCEC used to answer for us (power
    status, menu status, vendor ID) and Feature Aborts the rest
  - Sonos and WiFi calls run on their own threads, so a slow speaker never
    delays the replies the TV is waiting for
  - Keeps asking a TV that hasn't identified itself for its brand, and
    declines ARC until it has (accepting ARC makes a Samsung drop the bridge)

Key improvements over v1.4.0:
  - Auto TV brand detection via CEC Vendor ID (opcode 0x87)
  - Samsung (Anynet+): ARC declined — Samsung drops connection if accepted
  - LG (SimpLink):     ARC accepted — LG won't recognise audio system otherwise
  - LG reconnect handling: when LG periodically terminates ARC (normal behaviour),
    we acknowledge, re-assert System Audio Mode, and re-accept the next initiation
  - Handles LG's non-standard 0x8B Vendor Remote Button Up (key release)
  - Works with any TV brand; other brands get ARC accepted once the TV
    has identified itself

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
    8D    = Menu Request
    8F    = Give Device Power Status
    A0    = Vendor Command With ID (declined, Samsung's "powered up" included)
    44:40 / 44:6B / 44:6D = Power keys (ignored - never switch the TV input)

  Outgoing:
    72:01 = Set System Audio Mode ON
    7A:xx = Report Audio Status
    7E:01 = System Audio Mode Status ON
    C1    = Report ARC Initiated (LG only)
    C2    = Report ARC Terminated (LG only)
    00    = Feature Abort (Samsung ARC decline, unsupported messages)
    87    = Device Vendor ID (when the TV announces its own)
    86    = Set Stream Path (hands the screen back when the TV switches to the bridge)
    8E    = Menu Status
    90:00 = Report Power Status ON

Hardware: Raspberry Pi Zero 2 W
  Samsung: use any non-ARC HDMI port
  LG:      use any non-ARC HDMI port, with the TV's Sound Out on Optical (or
           whatever the speaker uses), and turn on LG mode in the admin panel.
           Not the ARC port: the LG would send its sound there, and a speaker on
           optical would go silent.
"""

import subprocess
import json
import os
import sys
import time
import re
import signal
import logging
import logging.handlers
import errno
import fcntl
import glob
import queue
import select
import socket
import ssl
import base64
import hashlib
import html
import urllib.parse
import urllib.request
import struct
from threading import Thread, Lock, Event

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
wifi_fail_count = 0

# CEC System Audio Mode - smart keepalive
# Only reasserts when volume commands stop flowing
SAM_CHECK_INTERVAL = 120   # Check every 2 minutes
SAM_IDLE_THRESHOLD = 120   # Reassert after 2 minutes of silence
SAM_STARTUP_DELAY = 5      # Wait for cec-client to initialize

# Volume tracking
current_volume = 30
is_muted = False
volume_lock = Lock()
last_vol_time = 0
VOL_DEBOUNCE = 0.05

# Sonos calls run on their own thread, so a slow speaker never delays CEC replies
sonos_queue = queue.Queue()
background_threads = {}  # started once, even when run_bridge restarts

# CEC handles: the kernel device, or cec-client as a fallback
cec_dev = None
cec_proc = None
cec_lock = Lock()

# Linux kernel CEC API (include/uapi/linux/cec.h)
CEC_DEVICE = '/dev/cec0'
CEC_MSG = struct.Struct('=QQIIII16s7Bx')                # struct cec_msg
CEC_LOG_ADDRS = struct.Struct('=4sHBBII15s4s4s4s48sx')  # struct cec_log_addrs
CEC_EVENT = struct.Struct('=QII64s')                    # struct cec_event


def _cec_ioctl(direction, nr, size):
    """_IOC() for the 'a' ioctl group. direction: 1 = write, 2 = read, 3 = read/write."""
    return (direction << 30) | (size << 16) | (ord('a') << 8) | nr


CEC_ADAP_G_PHYS_ADDR = _cec_ioctl(2, 1, 2)
CEC_ADAP_S_LOG_ADDRS = _cec_ioctl(3, 4, CEC_LOG_ADDRS.size)
CEC_TRANSMIT = _cec_ioctl(3, 5, CEC_MSG.size)
CEC_RECEIVE = _cec_ioctl(3, 6, CEC_MSG.size)
CEC_DQEVENT = _cec_ioctl(3, 7, CEC_EVENT.size)
CEC_S_MODE = _cec_ioctl(1, 9, 4)

CEC_MODE_INITIATOR = 0x01
CEC_MODE_EXCL_FOLLOWER = 0x20
CEC_MODE_MONITOR = 0xE0      # listen only: what this adapter sends and receives
CEC_MODE_MONITOR_ALL = 0xF0  # listen only: the whole bus, if the hardware can
CEC_EVENT_STATE_CHANGE = 1
CEC_EVENT_LOST_MSGS = 2
CEC_EVENT_FL_DROPPED_EVENTS = 0x02
CEC_TX_STATUS_OK = 0x01
CEC_PHYS_ADDR_INVALID = 0xFFFF
CEC_OP_CEC_VERSION_1_4 = 5
CEC_OP_PRIM_DEVTYPE_AUDIOSYSTEM = 5
CEC_LOG_ADDR_TYPE_AUDIOSYSTEM = 4
CEC_OP_ALL_DEVTYPE_AUDIOSYSTEM = 0x08

# Pulse-Eight: the vendor ID cec-client announced for the bridge, so the TV
# keeps seeing the same device
BRIDGE_VENDOR_ID = 0x001582

# The HDMI port /dev/cec0 belongs to, as the kernel's display driver sees it.
# A TV cuts its HDMI connections for a moment while it wakes up.  A Pi Zero-3
# checks the connection only every 10 seconds, so the bridge used to drop off
# HDMI-CEC with it and come back about 10 seconds after the TV was on - like a
# device being switched on, which a Samsung then switches to.  Holding the
# connection ("on") stops the kernel checking it, so the bridge keeps its CEC
# address through those cuts; "detect" hands it back.  A reboot resets it.
HDMI_CONNECTOR_STATUS = '/sys/class/drm/card*-HDMI-A-1/status'

# Directed messages that need no answer: reports, key releases, and the power
# and view-on requests that must never pull the TV input over to the bridge
SILENT_OPCODES = {
    0x00,              # Feature Abort
    0x04, 0x0D,        # Image View On, Text View On
    0x36,              # Standby
    0x44, 0x45,        # User Control Pressed (volume handled separately), Released
    0x47,              # Set OSD Name
    0x72, 0x7A, 0x7E,  # Set System Audio Mode, Report Audio Status, System Audio Mode Status
    0x8A, 0x8B,        # Vendor Remote Button Down / Up
    0x8E, 0x90, 0x9E,  # Menu Status, Report Power Status, CEC Version
    0xC1, 0xC2,        # Report ARC Initiated / Terminated
}

# Smart keepalive tracking
last_volume_command_time = 0
last_volume_lock = Lock()

# Handing the screen back.  Something the bridge can't see can send the TV to its
# input some seconds after another device takes the screen: a Fire TV remote with
# the wrong HDMI input saved (Equipment Control) does it by infrared.  The bridge
# never asks for it.  The TV announces the switch, so the bridge answers it with
# <Set Stream Path> for the device the TV came from, which makes that device
# claim the screen again.  Only shortly after a device took the screen, so the
# bridge's own screen can still be picked from the TV's source list later on.
SNAP_BACK_WINDOW = 60      # seconds after another device took the screen
SNAP_BACK_DELAY = 1.0      # let the TV finish switching first
SNAP_BACK_LIMIT = 3        # at most this many hand-backs...
SNAP_BACK_PERIOD = 600     # ...in this many seconds, in case the TV insists
screen_owner = None        # (physical address, time) of the device that last took the screen
pending_snap_back = None   # (physical address, due time)
snap_back_times = []

# The bridge's picture (the splash screen with the admin panel's address) is on
# only while the TV shows the bridge's input because someone chose it.  The rest
# of the time the TV gets no picture from the bridge - nothing to switch to -
# while HDMI-CEC, and so volume control, carries on (the kernel's CEC keeps its
# own power).  Written to the framebuffer's blank switch, which the display
# driver turns into switching the HDMI output off (4) and on (0).
FRAMEBUFFER_BLANK = '/sys/class/graphics/fb0/blank'
PICTURE_REFRESH = 60       # seconds between re-applying it, in case something turned it back on
picture_wanted = False
picture_applied_at = 0

# TV brand detection (set from CEC Vendor ID opcode 0x87)
# Controls ARC accept/decline behaviour
tv_brand = 'unknown'   # 'samsung', 'lg', ..., 'other', or 'unknown' until the TV says
tv_brand_lock = Lock()
last_vendor_query = 0
VENDOR_QUERY_INTERVAL = 10  # seconds between re-asking an unidentified TV

# Known CEC vendor IDs (3-byte, uppercase hex joined by colons)
TV_VENDORS = {
    '00:00:F0': 'samsung',
    '00:E0:91': 'lg',
    '00:08:00': 'sony',
    '00:90:F5': 'philips',
    '00:00:39': 'toshiba',
}

# CEC activity log: every message to and from the bridge in plain English, for the
# admin panel's CEC Activity page (sonosbridge.local/cec)
TRAFFIC_LOG_FILE = '/var/log/cec-sonos-bridge-cec.log'
traffic_log = logging.getLogger('cec_traffic')
traffic_log.propagate = False             # its own file, not the main log
traffic_monitor_active = False            # the monitor logs the traffic; without it, the bridge does
bridge_phys_addr = CEC_PHYS_ADDR_INVALID  # the bridge's TV input, e.g. 0x2000 = HDMI 2

DEVICE_NAMES = ('TV', 'Recorder 1', 'Recorder 2', 'Tuner 1', 'Player 1', 'Bridge',
                'Tuner 2', 'Tuner 3', 'Player 2', 'Recorder 3', 'Tuner 4', 'Player 3',
                'Backup 1', 'Backup 2', 'Specific', 'Unregistered')

OPCODE_NAMES = {
    0x00: 'Feature Abort', 0x04: 'Image View On', 0x0D: 'Text View On',
    0x32: 'Set Menu Language', 0x36: 'Standby', 0x44: 'Key Pressed', 0x45: 'Key Released',
    0x46: 'Give OSD Name', 0x47: 'Set OSD Name', 0x70: 'System Audio Mode Request',
    0x71: 'Give Audio Status', 0x72: 'Set System Audio Mode', 0x7A: 'Report Audio Status',
    0x7D: 'Give System Audio Mode Status', 0x7E: 'System Audio Mode Status',
    0x80: 'Routing Change', 0x81: 'Routing Information', 0x82: 'Active Source',
    0x83: 'Give Physical Address', 0x84: 'Report Physical Address',
    0x85: 'Request Active Source', 0x86: 'Set Stream Path', 0x87: 'Device Vendor ID',
    0x89: 'Vendor Command', 0x8A: 'Vendor Button Down', 0x8B: 'Vendor Button Up',
    0x8C: 'Give Device Vendor ID', 0x8D: 'Menu Request', 0x8E: 'Menu Status',
    0x8F: 'Give Device Power Status', 0x90: 'Report Power Status', 0x91: 'Get Menu Language',
    0x9D: 'Inactive Source', 0x9E: 'CEC Version', 0x9F: 'Get CEC Version',
    0xA0: 'Vendor Command With ID', 0xA3: 'Report Short Audio Descriptor',
    0xA4: 'Request Short Audio Descriptor', 0xA5: 'Give Features', 0xA6: 'Report Features',
    0xA7: 'Request Current Latency', 0xA8: 'Report Current Latency', 0xC0: 'Initiate ARC',
    0xC1: 'Report ARC Initiated', 0xC2: 'Report ARC Terminated',
    0xC3: 'Request ARC Initiation', 0xC4: 'Request ARC Termination', 0xC5: 'Terminate ARC',
    0xF8: 'CDC Message', 0xFF: 'Abort',
}

KEY_NAMES = {
    0x00: 'Select', 0x01: 'Up', 0x02: 'Down', 0x03: 'Left', 0x04: 'Right',
    0x09: 'Root Menu', 0x0A: 'Setup Menu', 0x0B: 'Contents Menu', 0x0D: 'Exit',
    0x40: 'Power', 0x41: 'Volume Up', 0x42: 'Volume Down', 0x43: 'Mute', 0x44: 'Play',
    0x45: 'Stop', 0x46: 'Pause', 0x65: 'Mute Function', 0x66: 'Restore Volume',
    0x6B: 'Power Toggle', 0x6C: 'Power Off', 0x6D: 'Power On',
}

DEVICE_TYPES = ('TV', 'Recorder', 'Reserved', 'Tuner', 'Player', 'Audio System', 'Switch',
                'Processor')
POWER_STATES = ('on', 'standby', 'turning on', 'turning off')
ABORT_REASONS = ('unrecognized', 'not in correct mode', 'cannot provide source',
                 'invalid operand', 'refused', 'unable to determine')
CEC_VERSIONS = {4: '1.3a', 5: '1.4', 6: '2.0'}

# Messages that choose what the TV shows: (opcode, offset of the physical address)
INPUT_OPCODES = {0x80: 2, 0x81: 0, 0x82: 0, 0x86: 0}


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
        tell_lg_tv()
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
        tell_lg_tv()
    except Exception as e:
        log.error(f"Mute error: {e}")


def read_sonos_level(speaker_ip):
    """The speaker's (volume, muted), read from the speaker itself; also updates our copy."""
    global current_volume, is_muted
    import soco
    speaker = soco.SoCo(speaker_ip)
    volume, muted = speaker.volume, speaker.mute
    with volume_lock:
        current_volume = volume
        is_muted = muted
    return volume, muted


def set_sonos_level(speaker_ip, volume, muted):
    """Set the speaker to an exact volume and mute (LG mode), changing only what differs.
    Returns True if it changed anything."""
    global current_volume, is_muted
    with volume_lock:
        same_volume, same_mute = current_volume == volume, is_muted == muted
    if same_volume and same_mute:
        return False
    import soco
    speaker = soco.SoCo(speaker_ip)
    if not same_volume:
        speaker.volume = volume
    if not same_mute:
        speaker.mute = muted
    with volume_lock:
        current_volume = volume
        is_muted = muted
    report_audio_status()
    return True


def tell_lg_tv():
    """LG mode: the TV remote's HDMI-CEC keys changed the speaker, so give the TV the
    same number (only while connected to an LG TV; otherwise nothing happens)."""
    follower = lg_follower
    if follower is not None:
        with volume_lock:
            level = (current_volume, is_muted)
        follower.speaker_changed(*level)


def parse_tx_command(command):
    """'tx 5F:72:01' -> b'\\x5f\\x72\\x01'"""
    return bytes(int(b, 16) for b in command.split(None, 1)[1].split(':'))


def format_cec_frame(frame):
    """b'\\x05\\x71' -> '>> 05:71', the cec-client traffic format the handlers parse."""
    return '>> ' + ':'.join(f'{b:02x}' for b in frame)


def send_cec_command(command):
    """Send a CEC command in cec-client 'tx' syntax, e.g. 'tx 5F:72:01'."""
    global cec_proc
    with cec_lock:
        if cec_dev:
            frame = parse_tx_command(command)
            acked = cec_dev.transmit(frame)
            log.info(f"CEC TX: {command}" + ("" if acked else " (not acknowledged)"))
            if not traffic_monitor_active:
                log_cec_traffic(frame, sent=True, acked=acked)
        elif cec_proc and cec_proc.stdin:
            try:
                cec_proc.stdin.write(command + "\n")
                cec_proc.stdin.flush()
                log.info(f"CEC TX: {command}")
            except Exception as e:
                log.error(f"Failed to send CEC command '{command}': {e}")


def report_audio_status(destination='0'):
    """Report current volume/mute (opcode 0x7A) to the TV, or to the device that asked.
    Bit 7 = mute, Bits 6-0 = volume percentage.
    """
    with volume_lock:
        vol = current_volume & 0x7F
        if is_muted:
            vol |= 0x80
    send_cec_command(f"tx 5{destination}:7A:{vol:02X}")


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
        detected = TV_VENDORS.get(vendor_id, 'other')
        if tv_brand != detected:
            tv_brand = detected
            log.info(f"TV brand detected: {tv_brand} (vendor ID {vendor_id})")


def arc_accept_mode():
    """Return True if this TV brand requires ARC to be accepted."""
    with tv_brand_lock:
        brand = tv_brand
    # Samsung (Anynet+) must have ARC declined, and so must a TV that hasn't
    # identified itself yet - it may be a Samsung.
    # Everything else (LG, Sony, other brands) gets ARC accepted.
    return brand not in ('samsung', 'unknown')


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

    match = re.search(r'>>\s*([0-9a-fA-F])[0-9a-fA-F]:(.+)', line)
    if not match:
        return False

    # Answer queries to whoever asked (the TV, or a player like the Fire TV);
    # an unregistered sender (F) can't be addressed, so tell the TV instead
    sender = match.group(1).upper()
    reply_to = sender if sender != 'F' else '0'
    data = match.group(2).strip().upper()
    opcode = data[:2]  # ignore extra operands, as CEC followers must

    # 0x70 - System Audio Mode Request -> respond ON + report volume
    if opcode == '70':
        log.info("CEC RX: System Audio Mode Request -> ON")
        send_cec_command("tx 5F:72:01")
        report_audio_status()
        return True

    # 0x71 - Give Audio Status -> report volume
    if opcode == '71':
        log.info("CEC RX: Give Audio Status -> reporting")
        report_audio_status(reply_to)
        return True

    # 0x7D - Give System Audio Mode Status -> ON
    if opcode == '7D':
        log.info("CEC RX: Give System Audio Mode Status -> ON")
        send_cec_command(f"tx 5{reply_to}:7E:01")
        return True

    # 0x8B - LG Vendor Remote Button Up (non-standard key release)
    # LG sends this instead of the standard 0x45 User Control Released.
    if opcode == '8B':
        log.debug("CEC RX: LG Vendor Remote Button Up (0x8B) - ignored")
        return True

    # 0xC0 / 0xC3 - Request ARC Initiation
    if opcode in ('C0', 'C3'):
        if arc_accept_mode():
            log.info(f"CEC RX: ARC Initiation (0x{opcode}) -> ACCEPTED (LG/other brands)")
            send_cec_command("tx 50:C1")    # Report ARC Initiated
        else:
            log.info(f"CEC RX: ARC Initiation (0x{opcode}) -> DECLINED (Samsung or unidentified TV)")
            send_cec_command(f"tx 50:00:{opcode}:00")  # Feature Abort
        return True

    # 0xC4 - Request ARC Termination
    if opcode == 'C4':
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
            log.info("CEC RX: ARC Termination -> declined (Samsung or unidentified TV)")
            send_cec_command("tx 50:00:C4:00")  # Feature Abort
        return True

    # 0xA4 - Request Short Audio Descriptor -> decline (not supported)
    if opcode == 'A4':
        log.info("CEC RX: Request Short Audio Descriptor -> Feature Abort")
        send_cec_command("tx 50:00:A4:00")
        return True

    return False


def process_cec_line(line, speaker_ip):
    """Handle one incoming message in cec-client traffic format ('>> 05:44:41').

    Returns True if it was a handshake or volume message we dealt with.
    """
    global last_vol_time

    # Detect TV brand from vendor ID broadcast (opcode 0x87)
    detect_tv_brand(line)

    # Handle handshake messages first
    if handle_cec_handshake(line):
        return True

    # Volume commands from ANY source device
    now = time.time()

    if ":44:41" in line:
        if now - last_vol_time > VOL_DEBOUNCE:
            last_vol_time = now
            sonos_queue.put((handle_volume, (speaker_ip, "up")))
        return True

    elif ":44:42" in line:
        if now - last_vol_time > VOL_DEBOUNCE:
            last_vol_time = now
            sonos_queue.put((handle_volume, (speaker_ip, "down")))
        return True

    elif ":44:43" in line:
        if now - last_vol_time > VOL_DEBOUNCE:
            last_vol_time = now
            sonos_queue.put((handle_mute, (speaker_ip,)))
        return True

    return False


def answer_core_message(frame):
    """Answer what cec-client used to answer for us, and Feature Abort the rest.

    Nothing here may send <Active Source> or <Image View On>: an audio system
    has no picture, so it must never pull the TV input away from the Fire TV.
    """
    if len(frame) < 2:
        return
    initiator, destination, opcode = frame[0] >> 4, frame[0] & 0x0F, frame[1]

    if destination == 0x0F:
        # The TV announced its vendor ID: announce ours back, as libCEC did
        if opcode == 0x87 and initiator == 0:
            vendor = ':'.join(f'{b:02X}' for b in BRIDGE_VENDOR_ID.to_bytes(3, 'big'))
            send_cec_command(f"tx 5F:87:{vendor}")
        # The TV announced its address (it just powered on): so do we, as libCEC did
        if opcode == 0x84 and initiator == 0:
            pa = cec_dev.physical_address()
            if pa != CEC_PHYS_ADDR_INVALID:
                send_cec_command(f"tx 5F:84:{pa >> 8:02X}:{pa & 0xFF:02X}:05")  # 05 = Audio System
        return

    if destination != 5 or initiator == 0x0F:
        return

    reply = f"tx 5{initiator:X}"
    if opcode == 0x8F:
        send_cec_command(f"{reply}:90:00")                 # Report Power Status: on
    elif opcode == 0x8D:
        state = '01' if frame[2:3] == b'\x01' else '00'    # deactivate -> deactivated
        send_cec_command(f"{reply}:8E:{state}")            # Menu Status
    elif opcode in (0x89, 0xA0):
        # Vendor commands, including the "powered up" one a Samsung sends after
        # waking (A0 00:00:F0 23).  Samsung's soundbars answer it with 24:00:80,
        # and Samsung TVs switch to their soundbars' inputs after waking, so the
        # bridge - a Pulse-Eight device - declines it like any other vendor's.
        send_cec_command(f"{reply}:00:{opcode:02X}:03")    # Feature Abort: invalid operand, as libCEC did
    elif opcode not in SILENT_OPCODES:
        send_cec_command(f"{reply}:00:{opcode:02X}:00")    # Feature Abort: unrecognized opcode


def handle_cec_frame(frame, speaker_ip):
    """Handle one frame received from the kernel CEC device."""
    if not traffic_monitor_active:
        log_cec_traffic(frame, sent=False)
    if frame[0] >> 4 == 0:
        ask_tv_vendor_id()  # the TV is awake, so it can tell us its brand
    watch_tv_routing(frame)
    if not process_cec_line(format_cec_frame(frame), speaker_ip):
        answer_core_message(frame)


def set_picture(on):
    """Turn the bridge's picture on (the TV is showing its input on purpose) or off."""
    global picture_wanted
    if on != picture_wanted:
        state = "on: the TV is showing the bridge's input" if on else "off"
        log.info(f"Splash screen {state}")
        traffic_log.info(f"--- Splash screen {state} ---")
    picture_wanted = on
    apply_picture()


def apply_picture():
    """Write picture_wanted to the display driver (also every PICTURE_REFRESH seconds)."""
    global picture_applied_at
    picture_applied_at = time.time()
    try:
        with open(FRAMEBUFFER_BLANK, 'w') as f:
            f.write('0' if picture_wanted else '4')  # FB_BLANK_UNBLANK / FB_BLANK_POWERDOWN
    except OSError as e:
        log.warning(f"Could not turn the splash screen {'on' if picture_wanted else 'off'}: {e}")


def watch_tv_routing(frame):
    """Keep track of which device has the screen: show the bridge's picture while the
    TV shows its input on purpose, and notice the TV being sent to it soon after
    another device took the screen (see SNAP_BACK_WINDOW)."""
    global screen_owner, pending_snap_back
    initiator, opcode = frame[0] >> 4, frame[1] if len(frame) > 1 else None
    came_from = None
    if opcode == 0x36 and initiator == 0:
        if picture_wanted:
            set_picture(False)                       # <Standby>: the TV is switching off
        return
    if opcode == 0x82 and len(frame) == 4 and initiator != 5:
        target = (frame[2] << 8) | frame[3]          # <Active Source> from a player
    elif opcode == 0x86 and len(frame) == 4 and initiator == 0:
        target = (frame[2] << 8) | frame[3]          # <Set Stream Path> from the TV
    elif opcode == 0x80 and len(frame) == 6 and initiator == 0:
        came_from = (frame[2] << 8) | frame[3]       # <Routing Change> from the TV: old path,
        target = (frame[4] << 8) | frame[5]          # new path
    else:
        return
    bridge_pa = cec_dev.physical_address() if cec_dev else CEC_PHYS_ADDR_INVALID
    if bridge_pa == CEC_PHYS_ADDR_INVALID:
        return
    now = time.time()
    if target != bridge_pa:
        # An HDMI device took the screen - or the TV's own apps (0.0.0.0) did
        screen_owner = (target, now) if target not in (0x0000, CEC_PHYS_ADDR_INVALID) else None
        pending_snap_back = None                   # the TV has moved on
        if picture_wanted:
            set_picture(False)
    elif opcode != 0x82 and pending_snap_back is None:
        if (screen_owner and now - screen_owner[1] <= SNAP_BACK_WINDOW
                and came_from in (None, screen_owner[0])):
            pending_snap_back = (screen_owner[0], now + SNAP_BACK_DELAY)  # sent here, not chosen
        elif not picture_wanted:
            set_picture(True)                      # chosen on purpose: show the splash screen


def snap_back_if_due():
    """Hand the screen back to the device the TV switched away from (see watch_tv_routing)."""
    global pending_snap_back
    if pending_snap_back is None or time.time() < pending_snap_back[1]:
        return
    target, _ = pending_snap_back
    pending_snap_back = None
    now = time.time()
    snap_back_times[:] = [t for t in snap_back_times if now - t < SNAP_BACK_PERIOD]
    pa = format_physical_address(target)
    if len(snap_back_times) >= SNAP_BACK_LIMIT:
        log.warning(f"TV keeps switching to the bridge - not handing the screen back to {pa} again")
        traffic_log.info(f"--- The TV keeps switching to the bridge: stopped handing the screen "
                         f"back to {pa} ---")
        set_picture(True)  # it stays here, so show the splash screen rather than "no signal"
        return
    snap_back_times.append(now)
    log.info(f"TV switched to the bridge soon after {pa} took the screen - handing it back")
    traffic_log.info(f"--- The TV switched to the bridge soon after the device at {pa} took the "
                     f"screen: asking it to take the screen back (a Fire TV with the wrong HDMI "
                     f"input saved under Equipment Control does this) ---")
    send_cec_command(f"tx 5F:86:{target >> 8:02X}:{target & 0xFF:02X}")  # <Set Stream Path>


def ask_tv_vendor_id(force=False):
    """Ask the TV for its vendor ID; its broadcast reply tells detect_tv_brand the brand.

    Repeats while the brand is unknown: at startup the TV may be off, and the
    kernel drops the TV's own power-on announcement if it arrives before our
    address is claimed.
    """
    global last_vendor_query
    with tv_brand_lock:
        brand = tv_brand
    now = time.time()
    if force or (brand == 'unknown' and now - last_vendor_query >= VENDOR_QUERY_INTERVAL):
        last_vendor_query = now
        send_cec_command("tx 50:8C")


def announce_to_tv(osd_name):
    """What libCEC did on startup: ask the TV for its vendor ID and give it our name."""
    ask_tv_vendor_id(force=True)
    name = osd_name.encode('ascii', 'ignore')[:14]
    if name:
        send_cec_command("tx 50:47:" + ":".join(f"{b:02X}" for b in name))


def format_physical_address(pa):
    """0x2000 -> '2.0.0.0' (TV input 2)"""
    return '.'.join(f'{(pa >> shift) & 0xF:x}' for shift in (12, 8, 4, 0))


def hold_hdmi_connection(hold):
    """Hold the HDMI connection as connected (True), or hand it back to the
    kernel's detection (False).  Returns True if the kernel took the setting."""
    paths = sorted(glob.glob(HDMI_CONNECTOR_STATUS))
    if not paths:
        log.warning("HDMI connector not found - can't hold the connection while the TV wakes")
        return False
    try:
        with open(paths[0], 'w') as f:
            f.write('on' if hold else 'detect')
    except OSError as e:
        log.warning(f"Could not {'hold' if hold else 'release'} the HDMI connection: {e}")
        return False
    return True


def update_hdmi_hold(held):
    """Hold the HDMI connection once the bridge has its TV input, and let go if the
    input is lost anyway (say the TV's EDID failed to read), so the kernel can find
    it again.  held: None until tried, then whether the hold took; returns the new value."""
    has_input = cec_dev.physical_address() != CEC_PHYS_ADDR_INVALID
    if held is None and has_input:
        apply_picture()  # the display driver may have turned the picture back on with the connection
        held = hold_hdmi_connection(True)
        if held:
            log.info("HDMI connection held: the bridge stays on HDMI-CEC while the TV wakes")
            traffic_log.info("--- Holding the HDMI connection through the TV's brief cuts "
                             "while it wakes up ---")
    elif held and not has_input:
        # Also the norm on a Pi 4/5: their hotplug interrupt bypasses the hold,
        # but they notice the connection coming back at once
        log.info("TV input lost while holding the HDMI connection - detecting it again")
        hold_hdmi_connection(False)
        held = None
    return held


def describe_operands(opcode, ops):
    """A message's operands in plain English, e.g. Active Source's physical address."""
    def pa(i=0):
        return format_physical_address((ops[i] << 8) | ops[i + 1])

    def text():
        return ops.decode('ascii', 'replace')

    if opcode in (0x81, 0x82, 0x86, 0x9D):
        return pa()
    if opcode == 0x80:
        return f"{pa(0)} to {pa(2)}"
    if opcode == 0x84:
        kind = DEVICE_TYPES[ops[2]] if ops[2] < len(DEVICE_TYPES) else f"type {ops[2]}"
        return f"{pa()} ({kind})"
    if opcode == 0x70:
        return f"for {pa()}"
    if opcode == 0x44:
        return KEY_NAMES.get(ops[0], f"key 0x{ops[0]:02X}")
    if opcode in (0x72, 0x7E):
        return 'On' if ops[0] else 'Off'
    if opcode == 0x7A:
        return f"volume {ops[0] & 0x7F}" + (", muted" if ops[0] & 0x80 else "")
    if opcode == 0x90:
        return POWER_STATES[ops[0]] if ops[0] < len(POWER_STATES) else f"state {ops[0]}"
    if opcode == 0x8D:
        return ('activate', 'deactivate', 'query')[ops[0]]
    if opcode == 0x8E:
        return ('activated', 'deactivated')[ops[0]]
    if opcode == 0x9E:
        return CEC_VERSIONS.get(ops[0], f"version {ops[0]}")
    if opcode in (0x32, 0x47):
        return repr(text())
    if opcode in (0x87, 0xA0):
        vendor = ':'.join(f'{b:02X}' for b in ops[:3])
        name = 'Pulse-Eight, the bridge' if vendor == '00:15:82' else TV_VENDORS.get(vendor)
        rest = ':'.join(f'{b:02X}' for b in ops[3:])
        return ' '.join(part for part in (vendor, f"({name})" if name else '', rest) if part)
    if opcode == 0x00:
        name = OPCODE_NAMES.get(ops[0], f"opcode 0x{ops[0]:02X}")
        reason = ABORT_REASONS[ops[1]] if ops[1] < len(ABORT_REASONS) else f"reason {ops[1]}"
        return f"of {name}: {reason}"
    return ':'.join(f'{b:02X}' for b in ops)


def describe_cec_frame(frame):
    """b'\\x4f\\x82\\x10\\x00' -> 'Player 1 -> all: Active Source 1.0.0.0'"""
    initiator, destination = frame[0] >> 4, frame[0] & 0x0F
    who = f"{DEVICE_NAMES[initiator]} -> {'all' if destination == 0x0F else DEVICE_NAMES[destination]}"
    if len(frame) < 2:
        return f"{who}: ping"
    opcode, ops = frame[1], bytes(frame[2:])
    text = f"{who}: {OPCODE_NAMES.get(opcode, f'opcode 0x{opcode:02X}')}"
    if opcode == 0x70 and not ops:
        text += " (off)"
    if ops:
        try:
            text += ' ' + describe_operands(opcode, ops)
        except IndexError:  # too few operands for this opcode
            text += ' ' + ':'.join(f'{b:02X}' for b in ops)
    return text


def input_switch_note(frame, sent):
    """Why this message could move the TV to the bridge's input, or None."""
    if len(frame) < 2:
        return None
    opcode = frame[1]
    if sent and opcode in (0x04, 0x0D, 0x82):
        return "THE BRIDGE ASKED THE TV TO SHOW IT - this should never happen"
    offset = INPUT_OPCODES.get(opcode)
    if offset is None or len(frame) < offset + 4 or bridge_phys_addr == CEC_PHYS_ADDR_INVALID:
        return None
    if (frame[offset + 2] << 8) | frame[offset + 3] == bridge_phys_addr:
        return "TV IS SWITCHING TO THE SONOS BRIDGE INPUT"
    return None


def log_cec_traffic(frame, sent, acked=True):
    """One line in the CEC activity log. Never raises: logging must not disturb the bridge."""
    try:
        _log_cec_traffic(frame, sent, acked)
    except Exception as e:
        log.warning(f"CEC activity log: could not record {bytes(frame).hex(':')}: {e}")


def _log_cec_traffic(frame, sent, acked):
    """Direction, raw bytes, and what they mean."""
    global bridge_phys_addr
    if len(frame) < 2:
        return  # pings: TVs check who is there every few seconds
    if sent and frame[1] == 0x84 and len(frame) >= 4:
        bridge_phys_addr = (frame[2] << 8) | frame[3]  # the bridge announcing its input
    destination = frame[0] & 0x0F
    direction = 'OUT' if sent else 'IN ' if destination in (5, 0x0F) else '   '
    raw = ':'.join(f'{b:02X}' for b in frame)
    description = describe_cec_frame(frame)
    line = f"{direction} {raw:<17} {description}"
    if sent and not acked:
        line += "  (not delivered)"
    note = input_switch_note(frame, sent)
    if note:
        line += f"   !! {note}"
        log.warning(f"CEC: {note}: {description}")
    traffic_log.info(line)


def log_cec_client_traffic(line):
    """cec-client prints received messages as '>> 05:44:41' and sent ones as '<< 50:7a:1e'."""
    match = re.search(r'(<<|>>)\s*([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2})*)', line)
    if match:
        log_cec_traffic(bytes.fromhex(match.group(2).replace(':', '')), sent=match.group(1) == '<<')


def describe_hdmi_state(phys_addr, log_addr_mask, changed_more_than_once=False):
    """A line for the activity log when the HDMI connection or the bridge's CEC address changes."""
    if phys_addr == CEC_PHYS_ADDR_INVALID:
        text = "HDMI connection down (TV off or cable unplugged)"
    else:
        claimed = "has" if log_addr_mask & (1 << 5) else "does not have"
        text = (f"HDMI connection up: bridge is on TV input {format_physical_address(phys_addr)}, "
                f"{claimed} the Audio System address")
    if changed_more_than_once:
        text += " (changed more than once)"
    return f"--- {text} ---"


def open_traffic_log():
    """Send the CEC activity log to its own small file (at most about 1 MB with its backup)."""
    if traffic_log.handlers:
        return
    try:
        handler = logging.handlers.RotatingFileHandler(TRAFFIC_LOG_FILE, maxBytes=512 * 1024,
                                                       backupCount=1)
    except Exception as e:
        log.warning(f"CEC activity log unavailable: {e}")
        return
    handler.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d  %(message)s', '%m-%d %H:%M:%S'))
    traffic_log.addHandler(handler)
    traffic_log.setLevel(logging.INFO)


def pack_cec_msg(frame=b'', timeout_ms=0):
    """struct cec_msg carrying frame (header, opcode, operands)."""
    return bytearray(CEC_MSG.pack(0, 0, len(frame), timeout_ms, 0, 0, bytes(frame),
                                  0, 0, 0, 0, 0, 0, 0))


def unpack_cec_msg(buf):
    """struct cec_msg -> (frame, tx_status)"""
    fields = CEC_MSG.unpack(buf)
    return fields[6][:fields[2]], fields[9]


def pack_log_addrs(osd_name):
    """struct cec_log_addrs claiming the Audio System address, or clearing if osd_name is None."""
    if osd_name is None:
        return bytearray(CEC_LOG_ADDRS.size)
    return bytearray(CEC_LOG_ADDRS.pack(
        bytes(4), 0, CEC_OP_CEC_VERSION_1_4, 1, BRIDGE_VENDOR_ID, 0,
        osd_name.encode('ascii', 'ignore')[:14],
        bytes([CEC_OP_PRIM_DEVTYPE_AUDIOSYSTEM, 0, 0, 0]),
        bytes([CEC_LOG_ADDR_TYPE_AUDIOSYSTEM, 0, 0, 0]),
        bytes([CEC_OP_ALL_DEVTYPE_AUDIOSYSTEM, 0, 0, 0]),
        bytes(48)))


class KernelCEC:
    """The Pi's HDMI-CEC adapter through the kernel CEC framework, as an Audio System.

    cec-client (libCEC) treats the Pi as a video source: it answers a power-on
    key or a routing message by broadcasting <Active Source>, which switches
    the TV to the Pi.  The kernel only sends what we tell it to.
    """

    def __init__(self, osd_name, device=CEC_DEVICE):
        self.fd = os.open(device, os.O_RDWR)
        try:
            # Exclusive follower: messages for us come here, while the kernel
            # still answers <Give Physical Address>, <Give OSD Name> and
            # <Give Device Vendor ID> itself
            fcntl.ioctl(self.fd, CEC_S_MODE,
                        struct.pack('=I', CEC_MODE_INITIATOR | CEC_MODE_EXCL_FOLLOWER))
            # Clear whatever cec-client left configured, then claim address 5
            fcntl.ioctl(self.fd, CEC_ADAP_S_LOG_ADDRS, pack_log_addrs(None))
            claim = pack_log_addrs(osd_name)
            fcntl.ioctl(self.fd, CEC_ADAP_S_LOG_ADDRS, claim)
            # With HDMI up, the kernel reports success even if address 5 was taken
            if (CEC_LOG_ADDRS.unpack(claim)[1] == 0 and
                    self.physical_address() != CEC_PHYS_ADDR_INVALID):
                raise OSError(errno.EADDRINUSE, "could not claim the Audio System address (5) - "
                                                "is a soundbar or receiver also on HDMI-CEC?")
        except OSError:
            os.close(self.fd)
            raise

    def physical_address(self):
        """HDMI physical address, 0x2000 = TV input 2 (CEC_PHYS_ADDR_INVALID if unplugged)."""
        buf = bytearray(2)
        fcntl.ioctl(self.fd, CEC_ADAP_G_PHYS_ADDR, buf)
        return struct.unpack('=H', buf)[0]

    def receive(self, timeout_ms):
        """Next frame addressed to us (or broadcast), or None after timeout_ms."""
        buf = pack_cec_msg(timeout_ms=timeout_ms)
        try:
            fcntl.ioctl(self.fd, CEC_RECEIVE, buf)
        except OSError as e:
            if e.errno in (errno.ETIMEDOUT, errno.EINTR):
                return None
            raise
        return unpack_cec_msg(buf)[0]

    def transmit(self, frame):
        """Send a frame; returns True if it was acknowledged."""
        buf = pack_cec_msg(frame)
        try:
            fcntl.ioctl(self.fd, CEC_TRANSMIT, buf)
        except OSError as e:
            # ENONET: no address claimed yet (TV off or HDMI unplugged)
            log.warning(f"CEC transmit failed: {e}")
            return False
        return bool(unpack_cec_msg(buf)[1] & CEC_TX_STATUS_OK)

    def close(self):
        try:
            fcntl.ioctl(self.fd, CEC_ADAP_S_LOG_ADDRS, pack_log_addrs(None))
        except OSError:
            pass
        os.close(self.fd)


def open_kernel_cec(osd_name):
    """KernelCEC for /dev/cec0, or None to fall back to cec-client when there is no such device.

    Setup errors are raised (main() retries), because cec-client on the same
    device would bring back the input switching.
    """
    if not os.path.exists(CEC_DEVICE):
        log.warning(f"{CEC_DEVICE} not found (legacy firmware CEC?) - using cec-client, "
                    "which can switch the TV to the bridge when a device sends it a power key")
        return None
    start_cec_monitor()  # first, so the activity log shows the bridge announcing itself
    return KernelCEC(osd_name)


class CECMonitor:
    """A second, listen-only handle on the CEC device, for the activity log.

    It sees every message the bridge sends and receives, including the replies
    the kernel makes on its own, and every change of HDMI connection (TV off or
    on, cable pulled).  With hardware that allows it, it sees the whole bus.
    """

    MAX_PER_READ = 100

    def __init__(self, device=CEC_DEVICE):
        self.fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
        try:
            try:
                fcntl.ioctl(self.fd, CEC_S_MODE, struct.pack('=I', CEC_MODE_MONITOR_ALL))
                self.whole_bus = True
            except OSError:
                fcntl.ioctl(self.fd, CEC_S_MODE, struct.pack('=I', CEC_MODE_MONITOR))
                self.whole_bus = False
        except OSError:
            os.close(self.fd)
            raise
        self.poller = select.poll()
        self.poller.register(self.fd, select.POLLIN | select.POLLPRI)

    def read(self, timeout_ms):
        """What happened since the last call, waiting up to timeout_ms for something:
        ('message', frame, sent, acked), ('state', phys_addr, log_addr_mask, changed_more_than_once)
        and ('lost', count) tuples, oldest first."""
        ready = self.poller.poll(timeout_ms)
        if not ready:
            return []
        revents = ready[0][1]
        if revents & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
            raise OSError(errno.ENODEV, "CEC device went away")
        stamped = []
        if revents & select.POLLPRI:
            stamped += self._dequeue(CEC_DQEVENT, CEC_EVENT.size, self._decode_event)
        if revents & select.POLLIN:
            stamped += self._dequeue(CEC_RECEIVE, CEC_MSG.size, self._decode_message)
        # messages and events come from separate queues: put them back in the order they happened
        return [item for _, item in sorted(stamped, key=lambda pair: pair[0])]

    def _dequeue(self, request, size, decode):
        """(kernel timestamp, item) for everything queued, until the queue is empty."""
        stamped = []
        for _ in range(self.MAX_PER_READ):
            buf = bytearray(size)
            try:
                fcntl.ioctl(self.fd, request, buf)
            except OSError as e:
                if e.errno == errno.EAGAIN:  # queue empty
                    break
                raise
            pair = decode(buf)
            if pair:
                stamped.append(pair)
        return stamped

    @staticmethod
    def _decode_message(buf):
        fields = CEC_MSG.unpack(buf)
        tx_ts, rx_ts, length, tx_status = fields[0], fields[1], fields[2], fields[9]
        frame = fields[6][:length]
        sent = bool(tx_status)
        return (tx_ts if sent else rx_ts,
                ('message', frame, sent, bool(tx_status & CEC_TX_STATUS_OK)))

    @staticmethod
    def _decode_event(buf):
        ts, event, flags, data = CEC_EVENT.unpack(buf)
        if event == CEC_EVENT_STATE_CHANGE:
            phys_addr, log_addr_mask = struct.unpack_from('=HH', data)
            return ts, ('state', phys_addr, log_addr_mask, bool(flags & CEC_EVENT_FL_DROPPED_EVENTS))
        if event == CEC_EVENT_LOST_MSGS:
            return ts, ('lost', struct.unpack_from('=I', data)[0])
        return None

    def close(self):
        os.close(self.fd)


def log_monitor_item(item):
    """Write one thing the monitor saw to the CEC activity log."""
    global bridge_phys_addr
    if item[0] == 'message':
        log_cec_traffic(*item[1:])
    elif item[0] == 'state':
        bridge_phys_addr = item[1]
        traffic_log.info(describe_hdmi_state(*item[1:]))
    elif item[0] == 'lost':
        traffic_log.info(f"--- {item[1]} messages came too fast to log ---")


def run_cec_monitor(monitor):
    """Thread: everything the monitor sees goes to the CEC activity log."""
    global traffic_monitor_active
    try:
        while True:
            for item in monitor.read(timeout_ms=5000):
                log_monitor_item(item)
    except Exception as e:
        log.warning(f"CEC activity monitor stopped: {e}")
    finally:
        traffic_monitor_active = False
        monitor.close()


def start_cec_monitor():
    """Start the activity log's monitor unless it is running (run_bridge restarts after errors)."""
    global traffic_monitor_active
    thread = background_threads.get('cec_monitor')
    if thread is not None and thread.is_alive():
        return
    try:
        monitor = CECMonitor()
    except Exception as e:
        log.warning(f"CEC activity monitor unavailable ({e}) - logging the bridge's own messages only")
        return
    traffic_monitor_active = True
    try:
        thread = Thread(target=run_cec_monitor, args=(monitor,), daemon=True)
        thread.start()
    except Exception as e:
        traffic_monitor_active = False
        monitor.close()
        log.warning(f"CEC activity monitor could not start ({e}) - logging the bridge's own messages only")
        return
    background_threads['cec_monitor'] = thread
    scope = "the whole HDMI bus" if monitor.whole_bus else "messages to and from the bridge"
    log.info(f"CEC activity log ({scope}): {TRAFFIC_LOG_FILE}")


# LG mode: follow an LG webOS TV's own volume and mute over Wi-Fi.
#
# An LG TV gives its remote's volume keys over HDMI-CEC only to a sound device on
# its ARC port, and a speaker on optical would go silent there.  So with LG mode
# on, the bridge follows the TV's own volume number through the TV's network API
# (webOS "second screen", SSAP, a WebSocket on port 3001 or 3000) and sets the
# speaker to the same number.  Off by default; with it off, the follower thread
# only waits, and nothing here touches the speaker or the network.

LG_TV_DEFAULTS = {'enabled': False, 'host': '', 'name': '', 'client_key': '', 'secure': None}
LG_PORTS = {True: 3001, False: 3000}  # wss:// (2018+ firmware; the only one from 2023) / ws://
LG_SSDP_ADDRESS = ('239.255.255.250', 1900)
LG_SSDP_ST = 'urn:lge-com:service:webos-second-screen:1'
LG_VOLUME_URI = 'ssap://audio/getVolume'

# The pairing request, copied verbatim from aiowebostv (Home Assistant's webOS library):
#   https://github.com/home-assistant-libs/aiowebostv/blob/f52c91bfe6c8ff1cd2639f59db8aa408320abe69/aiowebostv/handshake.py
#   Copyright the aiowebostv authors, Apache License 2.0
#   (https://github.com/home-assistant-libs/aiowebostv/blob/main/LICENSE)
LG_REGISTRATION_PAYLOAD = {
    "forcePairing": False,
    "manifest": {
        "appVersion": "1.1",
        "manifestVersion": 1,
        "permissions": [
            "APP_TO_APP",
            "CLOSE",
            "CONTROL_AUDIO",
            "CONTROL_DISPLAY",
            "CONTROL_INPUT_JOYSTICK",
            "CONTROL_INPUT_MEDIA_PLAYBACK",
            "CONTROL_INPUT_MEDIA_RECORDING",
            "CONTROL_INPUT_TEXT",
            "CONTROL_INPUT_TV",
            "CONTROL_MOUSE_AND_KEYBOARD",
            "CONTROL_POWER",
            "CONTROL_TV_SCREEN",
            "LAUNCH",
            "LAUNCH_WEBAPP",
            "READ_APP_STATUS",
            "READ_COUNTRY_INFO",
            "READ_CURRENT_CHANNEL",
            "READ_INPUT_DEVICE_LIST",
            "READ_INSTALLED_APPS",
            "READ_LGE_SDX",
            "READ_LGE_TV_INPUT_EVENTS",
            "READ_NETWORK_STATE",
            "READ_NOTIFICATIONS",
            "READ_POWER_STATE",
            "READ_RUNNING_APPS",
            "READ_SETTINGS",
            "READ_TV_CHANNEL_LIST",
            "READ_TV_CURRENT_TIME",
            "READ_UPDATE_INFO",
            "SEARCH",
            "TEST_OPEN",
            "TEST_PROTECTED",
            "TEST_SECURE",
            "UPDATE_FROM_REMOTE_APP",
            "WRITE_NOTIFICATION_ALERT",
            "WRITE_NOTIFICATION_TOAST",
            "WRITE_SETTINGS",
        ],
    },
    "pairingType": "PROMPT",
}

LG_HINT_ARC = ("Your TV is sending sound over HDMI ARC. For your Sonos, set Sound Out to Optical "
               "(Settings > Sound > Sound Out).")
LG_HINT_SPEAKERS = ("Your TV is playing sound through its own speakers. For your Sonos, set Sound Out "
                    "to Optical (Settings > Sound > Sound Out).")
LG_HINT_CONNECT_APPS = ("On the TV, turn on LG Connect Apps / TV On With Mobile (the name depends on the "
                        "TV's age: look under Settings > General, or Settings > Network).")

config_lock = Lock()  # shared with web_server.save_config (see start_web_server)
lg_follower = None    # the LGFollower, created by main()


def read_config_file():
    """config.json as a dict ({} if there is none yet)."""
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def write_config_atomic(config):
    """Write config.json through a temporary file, so a power cut never leaves half a file."""
    tmp = f'{CONFIG_FILE}.tmp'
    with open(tmp, 'w') as f:
        json.dump(config, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)


def load_lg_settings():
    """The LG TV settings from config.json, with defaults for anything missing."""
    settings = dict(LG_TV_DEFAULTS)
    try:
        stored = read_config_file().get('lg_tv')
    except (OSError, ValueError) as e:
        log.warning(f"LG TV: could not read the settings: {e}")
        stored = None
    if isinstance(stored, dict):
        settings.update({key: stored[key] for key in LG_TV_DEFAULTS if key in stored})
    return settings


def save_lg_settings(**changes):
    """Change LG TV settings: re-read config.json, update only its lg_tv part, write it
    atomically.  Returns the new settings."""
    with config_lock:
        config = read_config_file()  # a damaged file raises, rather than being overwritten
        settings = dict(LG_TV_DEFAULTS)
        if isinstance(config.get('lg_tv'), dict):
            settings.update({key: config['lg_tv'][key] for key in LG_TV_DEFAULTS
                             if key in config['lg_tv']})
        settings.update(changes)
        config['lg_tv'] = settings
        write_config_atomic(config)
    return settings


def is_valid_tv_host(host):
    """An IP address or host name, nothing that could smuggle anything else into a request."""
    return bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-]{0,252}', host or ''))


# --- WebSocket client (RFC 6455), standard library only ---

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
WS_MAX_MESSAGE = 1024 * 1024  # volume messages are tiny; refuse anything silly
WS_CONTINUATION, WS_TEXT, WS_BINARY, WS_CLOSE, WS_PING, WS_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    """The WebSocket handshake or a frame went wrong."""


class WebSocketClosed(WebSocketError):
    """The other end closed the connection."""


def websocket_accept_key(key):
    """What the server must answer in Sec-WebSocket-Accept for our Sec-WebSocket-Key."""
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def mask_ws_payload(payload, mask):
    """XOR payload with the 4-byte mask (masking and unmasking are the same)."""
    if not payload:
        return b''
    key = (mask * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, 'big') ^ int.from_bytes(key, 'big')).to_bytes(len(payload), 'big')


def encode_ws_frame(opcode, payload, mask=None, fin=True):
    """One client frame.  Clients always mask, with a random 4-byte mask."""
    mask = os.urandom(4) if mask is None else mask
    header = bytearray([(0x80 if fin else 0) | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 1 << 16:
        header.append(0x80 | 126)
        header += struct.pack('!H', length)
    else:
        header.append(0x80 | 127)
        header += struct.pack('!Q', length)
    return bytes(header) + mask + mask_ws_payload(payload, mask)


def decode_ws_frame(buf):
    """(fin, opcode, payload, size) of the first whole frame in buf, or None if it is incomplete."""
    if len(buf) < 2:
        return None
    length, pos = buf[1] & 0x7F, 2
    if length == 126:
        if len(buf) < 4:
            return None
        length, pos = struct.unpack_from('!H', buf, 2)[0], 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length, pos = struct.unpack_from('!Q', buf, 2)[0], 10
    if length > WS_MAX_MESSAGE:
        raise WebSocketError(f"frame too large ({length} bytes)")
    mask = None
    if buf[1] & 0x80:  # servers don't mask, but cope if one does
        if len(buf) < pos + 4:
            return None
        mask, pos = bytes(buf[pos:pos + 4]), pos + 4
    if len(buf) < pos + length:
        return None
    payload = bytes(buf[pos:pos + length])
    if mask:
        payload = mask_ws_payload(payload, mask)
    return bool(buf[0] & 0x80), buf[0] & 0x0F, payload, pos + length


class WebSocket:
    """A small WebSocket client: text messages in and out, pings answered, pongs noted."""

    def __init__(self, sock, received=b''):
        self.sock = sock
        self.buffer = bytearray(received)
        self.fragments = None      # (opcode, [parts]) while a fragmented message arrives
        self.send_lock = Lock()
        self.last_pong = time.monotonic()
        self.closed = False

    @classmethod
    def connect(cls, host, port, secure, timeout=5):
        """Open ws://host:port/ (or wss:// with the TV's self-signed certificate)."""
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if secure:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE  # TVs use self-signed certificates
                sock = context.wrap_socket(sock, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                          f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
            response = b''
            while b'\r\n\r\n' not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WebSocketError("connection closed during the WebSocket handshake")
                response += chunk
                if len(response) > 16384:
                    raise WebSocketError("WebSocket handshake answer too long")
            head, received = response.split(b'\r\n\r\n', 1)
            lines = head.decode('latin-1').split('\r\n')
            status = lines[0].split()
            if len(status) < 2 or status[1] != '101':
                raise WebSocketError(f"WebSocket handshake refused: {lines[0]}")
            headers = {}
            for line in lines[1:]:
                name, _, value = line.partition(':')
                headers[name.strip().lower()] = value.strip()
            if headers.get('sec-websocket-accept') != websocket_accept_key(key):
                raise WebSocketError("WebSocket handshake answer doesn't match (Sec-WebSocket-Accept)")
            return cls(sock, received)
        except BaseException:
            sock.close()
            raise

    def receive(self, timeout, interrupt=None):
        """The next text message; None after timeout seconds, or as soon as the interrupt
        socket has something to read.  Raises WebSocketClosed when the connection ends."""
        deadline = time.monotonic() + timeout
        while True:
            message = self._next_message()
            if message is not None:
                return message
            if not (isinstance(self.sock, ssl.SSLSocket) and self.sock.pending()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                watch = [self.sock] if interrupt is None else [self.sock, interrupt]
                readable = select.select(watch, [], [], remaining)[0]
                if not readable or interrupt in readable:
                    return None
            try:
                chunk = self.sock.recv(65536)
            except (ssl.SSLWantReadError, socket.timeout):
                continue  # half a TLS record so far
            if not chunk:
                self.closed = True
                raise WebSocketClosed("the connection closed")
            self.buffer += chunk

    def _next_message(self):
        """A whole text message from what has arrived, answering pings and closes on the way."""
        while True:
            frame = decode_ws_frame(self.buffer)
            if frame is None:
                return None
            fin, opcode, payload, size = frame
            del self.buffer[:size]
            if opcode == WS_PING:
                self._send(WS_PONG, payload)
            elif opcode == WS_PONG:
                self.last_pong = time.monotonic()
            elif opcode == WS_CLOSE:
                if not self.closed:
                    self.closed = True
                    try:
                        self._send(WS_CLOSE, payload[:2])
                    except OSError:
                        pass
                raise WebSocketClosed("the TV closed the connection")
            elif opcode in (WS_TEXT, WS_BINARY):
                if self.fragments is not None:
                    raise WebSocketError("a new message started before the last one ended")
                if not fin:
                    self.fragments = (opcode, [payload])
                elif opcode == WS_TEXT:
                    return payload.decode('utf-8')
            elif opcode == WS_CONTINUATION:
                if self.fragments is None:
                    raise WebSocketError("a message continued that never started")
                self.fragments[1].append(payload)
                if sum(len(part) for part in self.fragments[1]) > WS_MAX_MESSAGE:
                    raise WebSocketError("message too large")
                if fin:
                    first, parts = self.fragments
                    self.fragments = None
                    if first == WS_TEXT:
                        return b''.join(parts).decode('utf-8')
            else:
                raise WebSocketError(f"unknown WebSocket frame type {opcode}")

    def send_text(self, text):
        self._send(WS_TEXT, text.encode('utf-8'))

    def ping(self):
        self._send(WS_PING, b'')

    def _send(self, opcode, payload):
        with self.send_lock:
            self.sock.sendall(encode_ws_frame(opcode, payload))

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self._send(WS_CLOSE, struct.pack('!H', 1000))
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


# --- The TV's API (SSAP) ---

class LGPairingError(Exception):
    """The TV wouldn't let the bridge in.  reason: 'not_paired' (the TV wants to ask the
    owner, and nobody pressed Pair), 'refused', 'timeout' or 'cancelled'."""

    def __init__(self, reason, detail=''):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class LGTVConnection:
    """One connection to an LG TV's second-screen API."""

    def __init__(self, ws):
        self.ws = ws
        self.request_count = 0

    def send(self, msg_type, msg_id, uri=None, payload=None):
        message = {'type': msg_type, 'id': msg_id}
        if uri:
            message['uri'] = uri
        if payload is not None:
            message['payload'] = payload
        self.ws.send_text(json.dumps(message))

    def request(self, uri, payload=None):
        """Ask the TV to do something; its answer comes back with the same id (we don't wait)."""
        self.request_count += 1
        self.send('request', f'req_{self.request_count}', uri, payload or {})

    def subscribe_volume(self):
        """The TV then reports its volume and mute under id 'volume', now and on every change."""
        self.send('subscribe', 'volume', LG_VOLUME_URI)

    def register(self, client_key, allow_prompt, timeout=10, prompt_timeout=60, on_prompt=None,
                 should_stop=None):
        """Log in with client_key.  With allow_prompt, a TV that doesn't know the key asks its
        owner, and we wait up to prompt_timeout for them.  Returns the TV's client key."""
        payload = dict(LG_REGISTRATION_PAYLOAD)
        if client_key:
            payload['client-key'] = client_key
        self.send('register', 'register_0', payload=payload)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LGPairingError('timeout')
            text = self.ws.receive(min(remaining, 1.0))
            if should_stop and should_stop():
                raise LGPairingError('cancelled')
            try:
                message = json.loads(text) if text else None
            except ValueError:
                message = None
            if not isinstance(message, dict) or message.get('id', 'register_0') != 'register_0':
                continue
            answer = message.get('payload') if isinstance(message.get('payload'), dict) else {}
            if message.get('type') == 'registered':
                return answer.get('client-key') or client_key
            if message.get('type') == 'error':
                raise LGPairingError('refused', str(message.get('error', '')))
            if message.get('type') == 'response' and answer.get('pairingType') == 'PROMPT':
                if not allow_prompt:
                    raise LGPairingError('not_paired')
                if on_prompt:
                    on_prompt()
                deadline = time.monotonic() + prompt_timeout

    def close(self):
        self.ws.close()


def parse_lg_volume(payload):
    """(volume, muted, sound output) from a getVolume answer, old or new webOS; None for
    anything it doesn't say.  Volume is -1 while an ARC device owns the volume."""
    if not isinstance(payload, dict):
        return None, None, None
    status = payload.get('volumeStatus')
    status = status if isinstance(status, dict) else payload
    volume = status.get('volume')
    volume = int(volume) if isinstance(volume, (int, float)) and not isinstance(volume, bool) else None
    muted = None
    for source in (status, payload):
        for key in ('muteStatus', 'muted', 'mute'):
            if isinstance(source.get(key), bool):
                muted = source[key]
                break
        if muted is not None:
            break
    output = status.get('soundOutput') or payload.get('soundOutput') or payload.get('scenario')
    return volume, muted, output if isinstance(output, str) else None


def describe_lg_sound_output(output):
    """'external_optical' or 'mastervolume_ext_speaker_arc' -> 'Optical' or 'HDMI ARC'."""
    if not output:
        return None
    text = output.lower()
    for pattern, label in (('arc', 'HDMI ARC'), ('optical', 'Optical'), ('headphone', 'Headphones'),
                           ('bt_', 'Bluetooth'), ('bluetooth', 'Bluetooth'), ('lineout', 'Line out'),
                           ('tv_speaker', 'TV speakers')):
        if pattern in text:
            return label
    return output


def lg_sound_output_hint(volume, output):
    """What the owner should change on the TV, if the Sonos can't follow it like this."""
    label = describe_lg_sound_output(output)
    if volume == -1 or label == 'HDMI ARC':
        return LG_HINT_ARC
    if label == 'TV speakers':
        return LG_HINT_SPEAKERS
    return None


# --- Finding the TV (SSDP) ---

def parse_ssdp_reply(data, address):
    """An LG TV from one SSDP reply: {'host', 'name', 'location', 'uuid'}, or None."""
    text = data.decode('utf-8', 'replace')
    lines = text.replace('\r\n', '\n').split('\n')
    if not lines[0].upper().startswith('HTTP/') or ' 200' not in lines[0]:
        return None
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(':')
        if sep:
            headers[name.strip().lower()] = value.strip()
    if 'webos-second-screen' not in headers.get('st', '') + headers.get('usn', ''):
        return None
    uuid = headers.get('usn', '').split('::')[0]
    return {'host': address[0], 'name': '', 'location': headers.get('location', ''),
            'uuid': uuid[5:] if uuid.startswith('uuid:') else uuid}


def fetch_lg_tv_name(location, host, timeout=2):
    """The TV's friendly name from its UPnP description (only from the TV itself), or ''."""
    if urllib.parse.urlparse(location).hostname != host:
        return ''
    try:
        with urllib.request.urlopen(location, timeout=timeout) as response:
            text = response.read(65536).decode('utf-8', 'replace')
    except Exception:
        return ''
    match = re.search(r'<friendlyName>(.*?)</friendlyName>', text, re.S)
    return html.unescape(match.group(1)).strip()[:60] if match else ''


def discover_lg_tvs(timeout=3.0, fetch_names=True, sock=None):
    """LG webOS TVs answering an SSDP search within timeout seconds."""
    request = ('M-SEARCH * HTTP/1.1\r\n'
               f'HOST: {LG_SSDP_ADDRESS[0]}:{LG_SSDP_ADDRESS[1]}\r\n'
               'MAN: "ssdp:discover"\r\n'
               'MX: 2\r\n'
               f'ST: {LG_SSDP_ST}\r\n\r\n').encode()
    if sock is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    tvs = {}
    try:
        sock.sendto(request, LG_SSDP_ADDRESS)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, address = sock.recvfrom(4096)
            except socket.timeout:
                break
            tv = parse_ssdp_reply(data, address)
            if tv and tv['host'] not in tvs:
                tvs[tv['host']] = tv
    finally:
        sock.close()
    if fetch_names:
        for tv in tvs.values():
            if tv['location']:
                tv['name'] = fetch_lg_tv_name(tv['location'], tv['host'])
    return list(tvs.values())


# --- The follower ---

def take_echo(sent, value):
    """If value is one we sent the TV (sent: (value, time), oldest first), forget it and
    everything sent before it, and return True."""
    for i, (sent_value, _) in enumerate(sent):
        if sent_value == value:
            del sent[:i + 1]
            return True
    return False


class LGFollower:
    """LG mode: keeps the Sonos speaker's volume and mute in step with the LG TV's own.

    One daemon thread.  It waits while LG mode is off or no TV is paired; otherwise it
    connects to the TV, sets the TV to the speaker's level, then follows the TV's
    volume reports.  The speaker is only ever changed by jobs on the Sonos queue, at
    most one waiting at a time, each applying the latest level the TV reported - so
    holding a volume button costs a few Sonos calls, not one per step.
    """

    RETRY_FIRST = 5        # seconds before reconnecting, doubling each time...
    RETRY_MAX = 60         # ...up to this
    CONNECT_TIMEOUT = 5
    REGISTER_TIMEOUT = 10
    PAIR_TIMEOUT = 60      # how long the owner has to choose Allow on the TV
    PING_INTERVAL = 30
    PONG_TIMEOUT = 10
    READ_TIMEOUT = 1.0     # how often the connection loop looks up from reading
    SYNC_TIMEOUT = 5       # waiting for the Sonos queue to read the speaker's level
    SETTLE_TIME = 1.0      # log a change once the volume has been still this long
    ECHO_WINDOW = 2.0      # a report matching what we just sent the TV is its echo

    def __init__(self, speaker_ip, ports=None):
        self.speaker_ip = speaker_ip
        self.ports = dict(ports or LG_PORTS)
        self.lock = Lock()
        self.settings = load_lg_settings()
        self.state, self.message = 'off', "LG mode is off."
        self.logged_message = self.message  # nothing to say while LG mode stays off
        self.tv = {'volume': None, 'muted': None, 'sound_output': None}
        self.last_change = None       # time.time() of the last change the TV reported
        self.pairing = {'state': 'idle', 'message': ''}
        self.pair_failures = 0
        self.pair_request = None      # (host, name) when the owner pressed Pair
        self.needs_pairing = False    # the TV wants to ask its owner: wait for Pair
        self.target = None            # (volume, muted) the speaker should have
        self.job_queued = False
        self.sent_volumes, self.sent_mutes = [], []  # (value, time) we sent the TV
        self.outbox = None            # (volume, muted) to send the TV
        self.connected = False
        self.was_connected = False    # this attempt got as far as following the TV
        self.skip_first_report = False
        self.unsettled = None         # (volume, muted) not logged yet, and when it came
        self.reconfigure = False
        self.stopping = False
        self.thread = None
        self.changed = Event()
        self.wake_reader, self.wake_writer = socket.socketpair()
        self.wake_reader.setblocking(False)
        self.wake_writer.setblocking(False)

    # -- called from other threads --

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.stopping = False
            self.thread = Thread(target=self.run, name='lg_follower', daemon=True)
            self.thread.start()

    def stop(self, timeout=5):
        self.stopping = True
        self._poke()
        if self.thread is not None:
            self.thread.join(timeout)

    def close(self):
        """Stop for good (tests; the bridge keeps its follower until it exits)."""
        self.stop()
        self.wake_reader.close()
        self.wake_writer.close()

    def set_enabled(self, enabled):
        self._save(enabled=bool(enabled))
        with self.lock:
            if enabled:
                self.needs_pairing = False  # one more try with the saved key
            self.reconfigure = True
        self._poke()

    def forget(self):
        self._save(host='', name='', client_key='', secure=None)
        with self.lock:
            self.needs_pairing = False
            self.pair_failures = 0
            self.pairing = {'state': 'idle', 'message': ''}
            self.reconfigure = True
        self._poke()

    def request_pairing(self, host, name=''):
        """Start pairing with the TV at host (the result shows in status()['pairing'])."""
        host = (host or '').strip()
        if not is_valid_tv_host(host):
            raise ValueError(host)
        with self.lock:
            self.pair_request = (host, re.sub(r'[\x00-\x1f\x7f]', '', str(name or ''))[:60])
            self.pairing = {'state': 'connecting', 'message': f"Connecting to the TV at {host}..."}
            self.reconfigure = True
        self._poke()

    def find_tvs(self):
        return discover_lg_tvs()

    def speaker_changed(self, volume, muted):
        """The speaker changed by other means (HDMI-CEC keys): give the TV the same number."""
        with self.lock:
            if not self.connected:
                return
            self.outbox = (volume, muted)
        self._poke()

    def status(self):
        """Everything the admin panel shows, as plain data."""
        with self.lock:
            settings = self.settings
            tv = dict(self.tv)
            connected = self.connected
            result = {
                'enabled': bool(settings['enabled']),
                'host': settings['host'],
                'name': settings['name'],
                'paired': bool(settings['host'] and settings['client_key']),
                'state': self.state,
                'message': self.message,
                'connected': connected,
                'pairing': dict(self.pairing),
                'last_change_ago': (None if self.last_change is None
                                    else max(0, int(time.time() - self.last_change))),
            }
            failures = self.pair_failures
        with volume_lock:
            sonos = (current_volume, is_muted)
        hints = []
        if connected:
            hint = lg_sound_output_hint(tv['volume'], tv['sound_output'])
            if hint:
                hints.append(hint)
        if failures >= 2 or result['state'] == 'refused':
            hints.append(LG_HINT_CONNECT_APPS)
        result.update({
            'tv_volume': tv['volume'] if connected else None,
            'tv_muted': tv['muted'] if connected else None,
            'sound_output': describe_lg_sound_output(tv['sound_output']) if connected else None,
            'sonos_volume': sonos[0],
            'sonos_muted': sonos[1],
            'hints': hints,
        })
        return result

    def _save(self, **changes):
        settings = save_lg_settings(**changes)
        with self.lock:
            self.settings = settings

    def _poke(self):
        """Wake the follower thread, whether it is waiting or reading from the TV."""
        self.changed.set()
        try:
            self.wake_writer.send(b'.')
        except OSError:
            pass  # already full of wake-ups

    # -- the follower thread --

    def run(self):
        delay = self.RETRY_FIRST
        while not self.stopping:
            self.changed.clear()
            self._drain_wakeups()
            with self.lock:
                self.reconfigure = False
                request, self.pair_request = self.pair_request, None
                settings = dict(self.settings)
                needs_pairing = self.needs_pairing
            if request:
                self._pair(*request)
                continue
            if not settings['enabled']:
                self._set_state('off', "LG mode is off.")
                self.changed.wait()
                continue
            if not settings['host'] or not settings['client_key'] or needs_pairing:
                if self.state not in ('not_paired', 'pair_refused'):
                    self._set_state('not_paired', "Not paired with a TV yet: press Find my TV, then Pair.")
                self.changed.wait()
                continue
            self.was_connected = False
            try:
                self._follow(settings)
            except LGPairingError as e:
                if e.reason in ('not_paired', 'refused'):
                    # Don't keep asking on the TV: wait for the owner to press Pair
                    with self.lock:
                        self.needs_pairing = True
                    if e.reason == 'refused':
                        self._set_state('pair_refused', "The TV refused the bridge. Press Pair to try again.")
                    else:
                        self._set_state('not_paired', "The TV has forgotten the bridge. Press Pair, then "
                                                      "choose Allow on the TV.")
                    continue
                if e.reason == 'timeout':
                    self._set_state('unreachable', "The TV didn't answer. Trying again...")
            except ConnectionRefusedError:
                self._set_state('refused', "The TV refused the connection. It may be turning off or on, "
                                           "or LG Connect Apps is off on the TV.")
            except (OSError, WebSocketError) as e:
                if self.was_connected:
                    self._set_state('unreachable', "Lost the TV (turned off?). Trying again...")
                else:
                    self._set_state('unreachable', "Can't reach the TV: it's off, or not on this Wi-Fi.",
                                    detail=e)
            except Exception as e:
                if self.state != 'error':
                    log.exception(f"LG TV: unexpected error: {e}")
                self._set_state('error', f"Something went wrong: {e}")
            if self.stopping or self.reconfigure:
                delay = self.RETRY_FIRST
                continue
            if self.was_connected:
                delay = self.RETRY_FIRST
            self.changed.wait(delay)
            delay = min(delay * 2, self.RETRY_MAX)
        self._set_state('off', "Stopped.")

    def _set_state(self, state, message, detail=None):
        """Show a new state on the admin panel, and log it if the message is new (so
        retrying every minute while the TV is off doesn't fill the log)."""
        with self.lock:
            self.state, self.message = state, message
            if state == 'connecting' or message == self.logged_message:
                return
            self.logged_message = message
        log.info(f"LG TV: {message}" + (f" ({detail})" if detail else ""))

    def _set_pairing(self, state, message):
        with self.lock:
            self.pairing = {'state': state, 'message': message}

    def _interrupted(self):
        return self.stopping or self.reconfigure

    def _drain_wakeups(self):
        try:
            while self.wake_reader.recv(4096):
                pass
        except OSError:
            pass

    def _connect(self, host, secure_first):
        """Connect over wss:// then ws:// (or the one that worked last time first)."""
        order = (False, True) if secure_first is False else (True, False)
        errors = []
        for secure in order:
            try:
                ws = WebSocket.connect(host, self.ports[secure], secure, timeout=self.CONNECT_TIMEOUT)
            except (OSError, WebSocketError) as e:
                errors.append(e)
                continue
            return LGTVConnection(ws), secure
        # Only one of the two ports is open on most TVs: report the other error if there is one
        others = [e for e in errors if not isinstance(e, ConnectionRefusedError)]
        raise (others or errors)[0]

    def _follow(self, settings):
        """One connection: log in, set the TV to the speaker's level, then follow the TV."""
        host = settings['host']
        if self.state not in ('unreachable', 'refused', 'error'):  # retrying: keep showing why
            self._set_state('connecting', f"Connecting to the TV at {host}...")
        conn, secure = self._connect(host, settings['secure'])
        try:
            key = conn.register(settings['client_key'], allow_prompt=False,
                                timeout=self.REGISTER_TIMEOUT, should_stop=self._interrupted)
            if secure != settings['secure'] or key != settings['client_key']:
                self._save(secure=secure, client_key=key)
            with self.lock:
                self.connected = True
                self.tv = {'volume': None, 'muted': None, 'sound_output': None}
                self.sent_volumes, self.sent_mutes = [], []
                self.outbox = None
                self.skip_first_report = False
            self.was_connected = True
            self._set_state('connected', "Connected.", detail=settings['name'] or host)
            self._sync_tv_to_speaker(conn)
            conn.subscribe_volume()
            self._read_loop(conn)
        finally:
            with self.lock:
                self.connected = False
                self.outbox = None
            conn.close()
            self._log_settled(force=True)

    def _sync_tv_to_speaker(self, conn):
        """On connecting, the TV takes the speaker's level, so turning the TV on never makes
        the speaker jump."""
        done, level = Event(), []

        def read_speaker():
            try:
                level.append(read_sonos_level(self.speaker_ip))
            finally:
                done.set()

        sonos_queue.put((read_speaker, ()))
        if not done.wait(self.SYNC_TIMEOUT) or not level:
            log.warning("LG TV: could not read the Sonos volume, so the TV keeps its own")
            return
        self._send_level(conn, *level[0])
        # The first report may still carry the TV's old level: following it would make
        # the speaker jump.  The TV reports again once it has taken the new one.
        with self.lock:
            self.skip_first_report = True

    def _send_level(self, conn, volume, muted):
        """Set the TV to the speaker's level, remembering it so its echo is recognised."""
        now = time.monotonic()
        with self.lock:
            self.sent_volumes = [(v, t) for v, t in self.sent_volumes if now - t < self.ECHO_WINDOW]
            self.sent_mutes = [(m, t) for m, t in self.sent_mutes if now - t < self.ECHO_WINDOW]
            send_volume = volume != self.tv['volume']
            send_mute = muted != self.tv['muted']
            if send_volume:
                self.sent_volumes.append((volume, now))
            if send_mute:
                self.sent_mutes.append((muted, now))
        if send_volume:
            conn.request('ssap://audio/setVolume', {'volume': volume})
        if send_mute:
            conn.request('ssap://audio/setMute', {'mute': muted})

    def _read_loop(self, conn):
        """Follow the TV until told to stop or reconfigure, or the connection fails."""
        last_ping = time.monotonic()
        ping_sent = None
        while not self._interrupted():
            text = conn.ws.receive(self.READ_TIMEOUT, interrupt=self.wake_reader)
            self._drain_wakeups()
            if text is not None:
                self._on_message(text)
            with self.lock:
                outgoing, self.outbox = self.outbox, None
            if outgoing:
                self._send_level(conn, *outgoing)
            now = time.monotonic()
            if ping_sent is not None and conn.ws.last_pong >= ping_sent:
                ping_sent = None
            if ping_sent is None and now - last_ping >= self.PING_INTERVAL:
                conn.ws.ping()
                ping_sent = last_ping = now
            elif ping_sent is not None and now - ping_sent >= self.PONG_TIMEOUT:
                raise WebSocketError("the TV stopped answering")
            self._log_settled()

    def _on_message(self, text):
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, dict) or message.get('id') != 'volume':
            return
        if message.get('type') == 'response':
            self._on_volume(message.get('payload'))
        elif message.get('type') == 'error':
            log.warning(f"LG TV: the TV won't report its volume: {message.get('error')}")

    def _on_volume(self, payload):
        """The TV reported its volume: give the speaker the same level (via the Sonos queue)."""
        volume, muted, output = parse_lg_volume(payload)
        now = time.monotonic()
        with self.lock:
            before = self.tv
            self.tv = {'volume': volume,
                       'muted': muted if muted is not None else before['muted'],
                       'sound_output': output or before['sound_output']}
            if self.skip_first_report:
                self.skip_first_report = False
                self.sent_volumes, self.sent_mutes = [], []
                return
            if volume is None or volume < 0:
                return  # an ARC device owns the volume, or the TV didn't say: leave the speaker
            volume = min(volume, 100)
            # Our own setVolume / setMute coming back, possibly an older one than the latest
            # (the speaker has moved on since): each field either didn't change, or is an echo
            self.sent_volumes = [(v, t) for v, t in self.sent_volumes if now - t < self.ECHO_WINDOW]
            self.sent_mutes = [(m, t) for m, t in self.sent_mutes if now - t < self.ECHO_WINDOW]
            volume_echo = volume == before['volume'] or take_echo(self.sent_volumes, volume)
            mute_echo = muted is None or muted == before['muted'] or take_echo(self.sent_mutes, muted)
            if volume_echo and mute_echo:
                return
            with volume_lock:
                sonos = (current_volume, is_muted)
            target = (volume, muted if muted is not None else sonos[1])
            if target == sonos and not self.job_queued:
                return  # the speaker already has it
            self.target = target
            self.last_change = time.time()
            self.unsettled = (target, now)
            if self.job_queued:
                return  # the waiting job will apply the latest target
            self.job_queued = True
        sonos_queue.put((self._apply_target, ()))

    def _apply_target(self):
        """Sonos queue job: set the speaker to the latest level the TV reported."""
        with self.lock:
            self.job_queued = False
            target = self.target
        if target is not None:
            set_sonos_level(self.speaker_ip, *target)

    def _log_settled(self, force=False):
        """One log line per change, once the volume has been still for SETTLE_TIME."""
        with self.lock:
            if not self.unsettled or not (force or time.monotonic() - self.unsettled[1] >= self.SETTLE_TIME):
                return
            (volume, muted), self.unsettled = self.unsettled[0], None
        level = f"{volume}" + (", muted" if muted else "")
        log.info(f"LG TV volume {level} -> Sonos {level}")

    def _pair(self, host, name):
        """The owner pressed Pair: connect, and let the TV ask them to allow the bridge."""
        settings = self.settings
        same_tv = settings['host'] == host
        try:
            conn, secure = self._connect(host, settings['secure'] if same_tv else None)
        except ConnectionRefusedError:
            return self._pairing_failed(f"The TV at {host} refused the connection. Make sure it's on. "
                                        + LG_HINT_CONNECT_APPS)
        except (OSError, WebSocketError) as e:
            return self._pairing_failed(f"Couldn't reach a TV at {host}. Make sure it's on and on the "
                                        f"same Wi-Fi as the bridge.", detail=e)
        try:
            key = conn.register(settings['client_key'] if same_tv else '', allow_prompt=True,
                                timeout=self.REGISTER_TIMEOUT, prompt_timeout=self.PAIR_TIMEOUT,
                                on_prompt=lambda: self._set_pairing(
                                    'prompt', "Look at your TV and choose Allow."),
                                should_stop=lambda: self.stopping)
            if not key:
                raise LGPairingError('refused', "no client key")
            self._save(host=host, name=name, client_key=key, secure=secure)
            with self.lock:
                self.needs_pairing = False
                self.pair_failures = 0
            try:
                conn.request('ssap://system.notifications/createToast',
                             {'message': 'Sonos Bridge connected'})
            except OSError:
                pass
        except LGPairingError as e:
            if e.reason == 'cancelled':
                return None
            if e.reason == 'refused':
                return self._pairing_failed("The TV said no. Press Pair again and choose Allow on the TV.")
            return self._pairing_failed("The TV didn't answer in time. Press Pair again, then choose "
                                        "Allow on the TV within a minute.")
        except (OSError, WebSocketError) as e:
            return self._pairing_failed("The connection to the TV dropped while pairing. Press Pair "
                                        "to try again.", detail=e)
        finally:
            conn.close()
        self._set_pairing('paired', f"Paired with {name or 'the TV at ' + host}.")
        log.info(f"LG TV: paired with {name or host} ({host})")
        return None

    def _pairing_failed(self, message, detail=None):
        with self.lock:
            self.pair_failures += 1
        self._set_pairing('failed', message)
        log.warning(f"LG TV: pairing failed: {message}" + (f" ({detail})" if detail else ""))


def create_lg_follower(config):
    """The LG follower, created once.  Its thread starts with the bridge (run_bridge).
    Never raises: whatever happens here, HDMI-CEC must carry on."""
    global lg_follower
    if lg_follower is None:
        try:
            lg_follower = LGFollower(config['speaker_ip'])
        except Exception as e:
            log.error(f"LG mode unavailable: {e}")
    return lg_follower


def start_lg_follower():
    """Start the LG follower's thread (it waits quietly unless LG mode is on)."""
    try:
        if lg_follower:
            lg_follower.start()
    except Exception as e:
        log.error(f"LG mode unavailable: {e}")


def run_next_sonos_action():
    """Apply the next queued volume/mute change (blocks until there is one)."""
    action, args = sonos_queue.get()
    try:
        action(*args)
    except Exception as e:
        log.error(f"Sonos error: {e}")


def sonos_worker():
    """Sonos changes, one at a time, off the CEC thread."""
    while True:
        run_next_sonos_action()


def check_wifi():
    """One WiFi check; reboots after WIFI_FAIL_THRESHOLD failures in a row."""
    global wifi_fail_count
    if is_wifi_connected():
        wifi_fail_count = 0
    else:
        wifi_fail_count += 1
        log.warning(f"WiFi disconnected (count: {wifi_fail_count})")
        if wifi_fail_count >= WIFI_FAIL_THRESHOLD:
            log.error("WiFi lost too long, rebooting...")
            os.system('reboot')


def wifi_watchdog():
    """WiFi checks on their own thread, so a slow nmcli never delays CEC replies."""
    while True:
        time.sleep(WIFI_CHECK_INTERVAL)
        check_wifi()


def start_once(target):
    """Run target on a daemon thread unless it already is (run_bridge restarts after errors)."""
    thread = background_threads.get(target.__name__)
    if thread is None or not thread.is_alive():
        thread = Thread(target=target, daemon=True)
        thread.start()
        background_threads[target.__name__] = thread


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
        import web_server
        # This file runs as __main__, so web_server mustn't import it (that would be a
        # second copy with its own settings): hand it what the LG TV section needs instead
        web_server.lg_follower = lg_follower
        web_server.config_lock = config_lock
        log.info("Starting admin web server...")
        web_server.run_server(port=80)
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
    global cec_proc, cec_dev, bridge_phys_addr

    speaker_ip = config['speaker_ip']
    speaker_name = config.get('speaker_name', 'Sonos')
    hdmi_port = config.get('hdmi_port', '2')

    open_traffic_log()
    traffic_log.info("--- Sonos Bridge v1.6.0 starting ---")

    log.info("=" * 50)
    log.info("CEC-Sonos Bridge v1.6.0 Active")
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
    hdmi_held = None  # None until the bridge has its TV input, then whether the hold worked

    try:
        cec_dev = open_kernel_cec(osd_name)
        if not cec_dev:
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
        start_once(sonos_worker)
        start_once(wifi_watchdog)
        start_lg_follower()

        if cec_dev:
            bridge_phys_addr = cec_dev.physical_address()
            pa = format_physical_address(bridge_phys_addr)
            log.info(f"CEC: {CEC_DEVICE} as Audio System at HDMI address {pa}")
            announce_to_tv(osd_name)
            set_picture(False)  # until someone switches the TV to the bridge's input
            while True:
                hdmi_held = update_hdmi_hold(hdmi_held)
                frame = cec_dev.receive(timeout_ms=1000)
                if frame:
                    handle_cec_frame(frame, speaker_ip)
                snap_back_if_due()
                if time.time() - picture_applied_at >= PICTURE_REFRESH:
                    apply_picture()
        else:
            for line in cec_proc.stdout:
                line = line.strip()
                if not line:
                    continue

                log_cec_client_traffic(line)

                # Only process incoming CEC traffic
                if ">>" not in line:
                    continue

                process_cec_line(line, speaker_ip)

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        if cec_dev:
            with cec_lock:  # the keepalive thread may be transmitting
                cec_dev.close()
                cec_dev = None
        if hdmi_held:
            hold_hdmi_connection(False)  # the next start takes the TV input afresh
        if cec_proc:
            cec_proc.terminate()
            try:
                cec_proc.wait(timeout=5)
            except:
                cec_proc.kill()


def install_signal_handlers():
    """systemctl stop/restart sends SIGTERM: exit through run_bridge's cleanup,
    which releases the CEC address instead of leaving it claimed with nobody answering."""
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))


def main():
    """Main entry point."""
    log.info("CEC-Sonos Bridge v1.6.0 starting...")

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

    create_lg_follower(config)
    Thread(target=start_web_server, daemon=True).start()
    display_splash_screen()
    run_bridge(config)


if __name__ == "__main__":
    install_signal_handlers()
    while True:
        try:
            main()
        except Exception as e:
            log.exception(f"Bridge error: {e}")
            log.info("Restarting in 10 seconds...")
            time.sleep(10)
