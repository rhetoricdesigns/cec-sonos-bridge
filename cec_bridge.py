#!/usr/bin/env python3
"""
CEC-Sonos Bridge v1.5.1
Monitors HDMI-CEC for TV remote volume commands and controls Sonos speaker.
Also runs a web server for admin access at http://sonosbridge.local

Talks to the kernel's CEC device (/dev/cec0) directly, as a pure Audio System.
Falls back to cec-client when /dev/cec0 is missing (legacy firmware CEC).

Key improvements over v1.5.0:
  - Never takes over the TV input.  cec-client (libCEC) acts like a video
    source: when the Fire TV or the TV sends the Audio System a power-on key
    (e.g. pressing Home on the Fire TV remote), libCEC broadcasts
    <Active Source> for the Pi and the TV switches to the splash screen.
    The kernel device never does that on its own.
  - Answers the housekeeping messages libCEC used to answer for us (power
    status, menu status, vendor ID, Samsung's audio-system query) and
    Feature Aborts the rest
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
    A0    = Vendor Command With ID (Samsung audio-system query)
    44:40 / 44:6B / 44:6D = Power keys (ignored - never switch the TV input)

  Outgoing:
    72:01 = Set System Audio Mode ON
    7A:xx = Report Audio Status
    7E:01 = System Audio Mode Status ON
    C1    = Report ARC Initiated (LG only)
    C2    = Report ARC Terminated (LG only)
    00    = Feature Abort (Samsung ARC decline, unsupported messages)
    87    = Device Vendor ID (when the TV announces its own)
    8E    = Menu Status
    90:00 = Report Power Status ON

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
import errno
import fcntl
import queue
import struct
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


def _cec_ioctl(direction, nr, size):
    """_IOC() for the 'a' ioctl group. direction: 1 = write, 2 = read, 3 = read/write."""
    return (direction << 30) | (size << 16) | (ord('a') << 8) | nr


CEC_ADAP_G_PHYS_ADDR = _cec_ioctl(2, 1, 2)
CEC_ADAP_S_LOG_ADDRS = _cec_ioctl(3, 4, CEC_LOG_ADDRS.size)
CEC_TRANSMIT = _cec_ioctl(3, 5, CEC_MSG.size)
CEC_RECEIVE = _cec_ioctl(3, 6, CEC_MSG.size)
CEC_S_MODE = _cec_ioctl(1, 9, 4)

CEC_MODE_INITIATOR = 0x01
CEC_MODE_EXCL_FOLLOWER = 0x20
CEC_TX_STATUS_OK = 0x01
CEC_PHYS_ADDR_INVALID = 0xFFFF
CEC_OP_CEC_VERSION_1_4 = 5
CEC_OP_PRIM_DEVTYPE_AUDIOSYSTEM = 5
CEC_LOG_ADDR_TYPE_AUDIOSYSTEM = 4
CEC_OP_ALL_DEVTYPE_AUDIOSYSTEM = 0x08

# Pulse-Eight: the vendor ID cec-client announced for the bridge, so the TV
# keeps seeing the same device
BRIDGE_VENDOR_ID = 0x001582

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
            acked = cec_dev.transmit(parse_tx_command(command))
            log.info(f"CEC TX: {command}" + ("" if acked else " (not acknowledged)"))
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
    elif opcode == 0xA0 and frame[2:6] == b'\x00\x00\xf0\x23':
        # Samsung's audio-system query; same reply as libCEC's Samsung handler
        send_cec_command(f"{reply}:A0:00:00:F0:24:00:80")
    elif opcode in (0x89, 0xA0):
        send_cec_command(f"{reply}:00:{opcode:02X}:03")    # Feature Abort: invalid operand, as libCEC did
    elif opcode not in SILENT_OPCODES:
        send_cec_command(f"{reply}:00:{opcode:02X}:00")    # Feature Abort: unrecognized opcode


def handle_cec_frame(frame, speaker_ip):
    """Handle one frame received from the kernel CEC device."""
    if frame[0] >> 4 == 0:
        ask_tv_vendor_id()  # the TV is awake, so it can tell us its brand
    if not process_cec_line(format_cec_frame(frame), speaker_ip):
        answer_core_message(frame)


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
        log.warning(f"{CEC_DEVICE} not found (legacy firmware CEC?) - using cec-client")
        return None
    return KernelCEC(osd_name)


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
    global cec_proc, cec_dev

    speaker_ip = config['speaker_ip']
    speaker_name = config.get('speaker_name', 'Sonos')
    hdmi_port = config.get('hdmi_port', '2')

    log.info("=" * 50)
    log.info("CEC-Sonos Bridge v1.5.1 Active")
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

        if cec_dev:
            pa = format_physical_address(cec_dev.physical_address())
            log.info(f"CEC: {CEC_DEVICE} as Audio System at HDMI address {pa}")
            announce_to_tv(osd_name)
            while True:
                frame = cec_dev.receive(timeout_ms=1000)
                if frame:
                    handle_cec_frame(frame, speaker_ip)
        else:
            for line in cec_proc.stdout:
                line = line.strip()
                if not line:
                    continue

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
    log.info("CEC-Sonos Bridge v1.5.1 starting...")

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
    install_signal_handlers()
    while True:
        try:
            main()
        except Exception as e:
            log.exception(f"Bridge error: {e}")
            log.info("Restarting in 10 seconds...")
            time.sleep(10)
