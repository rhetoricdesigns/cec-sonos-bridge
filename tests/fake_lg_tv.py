"""
A fake LG webOS TV for the LG mode tests: a real WebSocket server on 127.0.0.1
(standard library only), speaking enough of the TV's second-screen API (SSAP):
pairing, volume reports, setVolume / setMute, toasts.  Optionally over TLS.

    tv = FakeLGTV(paired_keys={'k1'})
    tv.start()
    tv.press_volume(24)     # someone pressed volume on the LG remote
    tv.power_off()          # the connection drops and the port stops answering
    tv.power_on()           # same port again
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def server_frame(opcode, payload, fin=True):
    """An unmasked frame, as a server sends it."""
    header = bytearray([(0x80 if fin else 0) | opcode])
    if len(payload) < 126:
        header.append(len(payload))
    elif len(payload) < 1 << 16:
        header.append(126)
        header += struct.pack('!H', len(payload))
    else:
        header.append(127)
        header += struct.pack('!Q', len(payload))
    return bytes(header) + payload


def read_client_frame(sock, buffer):
    """(fin, opcode, payload, masked) of the next frame from a client; buffer is a bytearray
    of what has been read but not used.  None when the connection ends."""
    def need(n):
        while len(buffer) < n:
            chunk = sock.recv(65536)
            if not chunk:
                return False
            buffer.extend(chunk)
        return True

    if not need(2):
        return None
    length, pos = buffer[1] & 0x7F, 2
    if length == 126:
        if not need(4):
            return None
        length, pos = struct.unpack_from('!H', buffer, 2)[0], 4
    elif length == 127:
        if not need(10):
            return None
        length, pos = struct.unpack_from('!Q', buffer, 2)[0], 10
    masked = bool(buffer[1] & 0x80)
    mask = b''
    if masked:
        if not need(pos + 4):
            return None
        mask, pos = bytes(buffer[pos:pos + 4]), pos + 4
    if not need(pos + length):
        return None
    payload = bytes(buffer[pos:pos + length])
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    fin, opcode = bool(buffer[0] & 0x80), buffer[0] & 0x0F
    del buffer[:pos + length]
    return fin, opcode, payload, masked


def accept_websocket(sock, accept_override=None):
    """Read a client's handshake and answer 101.  Returns the request's headers."""
    request = b''
    while b'\r\n\r\n' not in request:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("client went away during the handshake")
        request += chunk
    lines = request.split(b'\r\n\r\n')[0].decode('latin-1').split('\r\n')
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(':')
        headers[name.strip().lower()] = value.strip()
    accept = base64.b64encode(hashlib.sha1(
        (headers['sec-websocket-key'] + WS_GUID).encode()).digest()).decode()
    sock.sendall((f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept_override or accept}\r\n\r\n")
                 .encode())
    return dict(headers, request_line=lines[0])


def make_self_signed_cert(directory):
    """(certfile, keyfile) made with the openssl command, or None if it isn't available."""
    cert, key = os.path.join(directory, 'tv.crt'), os.path.join(directory, 'tv.key')
    try:
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=lgwebostv', '-keyout', key, '-out', cert],
                       check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return cert, key


class FakeLGTV:
    """See the module docstring.  All state is read and changed under self.lock."""

    def __init__(self, paired_keys=(), on_prompt='accept', payload_format='new', tls=None,
                 volume=10, muted=False, sound_output='external_optical'):
        self.paired_keys = set(paired_keys)
        self.on_prompt = on_prompt            # 'accept', 'reject' or 'ignore'
        self.payload_format = payload_format  # 'new' (volumeStatus) or 'old'
        self.tls = tls                        # (certfile, keyfile) for wss://
        self.volume, self.muted, self.sound_output = volume, muted, sound_output
        self.answer_pings = True
        self.fragment_messages = False        # send each text message in three fragments
        self.bad_accept = False               # answer the handshake wrongly
        self.lock = threading.Lock()
        self.received = []                    # every SSAP message from clients
        self.toasts = []
        self.prompts = 0
        self.connections = 0
        self.unmasked_frames = 0
        self.port = None
        self.listener = None
        self.clients = []                     # [sock, send_lock, subscriptions]
        self.issued = 0

    # -- the test's controls --

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', self.port or 0))
        listener.listen(5)
        self.port = listener.getsockname()[1]
        self.listener = listener
        threading.Thread(target=self._accept_loop, args=(listener,), daemon=True).start()
        return self

    def power_off(self):
        """The TV switches off: connections drop and the port stops answering."""
        listener, self.listener = self.listener, None
        if listener:
            listener.close()
        with self.lock:
            clients, self.clients = self.clients, []
        for client in clients:
            try:
                client[0].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client[0].close()

    def power_on(self):
        self.start()

    stop = power_off

    def press_volume(self, volume):
        """The LG remote changed the volume."""
        with self.lock:
            self.volume = volume
        self._report()

    def set_mute(self, muted):
        with self.lock:
            self.muted = muted
        self._report()

    def report(self, **fields):
        """Change any of volume / muted / sound_output, then report it."""
        with self.lock:
            for name, value in fields.items():
                setattr(self, name, value)
        self._report()

    def requests(self, uri):
        with self.lock:
            return [m for m in self.received if m.get('uri') == uri]

    def connected(self):
        with self.lock:
            return len(self.clients)

    def wait_until(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    # -- the server --

    def _accept_loop(self, listener):
        while True:
            try:
                sock, _ = listener.accept()
            except OSError:
                return  # powered off
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        try:
            if self.tls:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(*self.tls)
                sock = context.wrap_socket(sock, server_side=True)
            accept_websocket(sock, 'wrong' if self.bad_accept else None)
        except (OSError, ssl.SSLError, ConnectionError, KeyError):
            sock.close()
            return
        client = [sock, threading.Lock(), set(), False]  # sock, send lock, subscriptions, registered
        with self.lock:
            self.clients.append(client)
            self.connections += 1
        buffer = bytearray()
        parts = []
        try:
            while True:
                frame = read_client_frame(sock, buffer)
                if frame is None:
                    break
                fin, opcode, payload, masked = frame
                if not masked:
                    with self.lock:
                        self.unmasked_frames += 1
                if opcode == 0x9:
                    if self.answer_pings:
                        self._send_frame(client, 0xA, payload)
                elif opcode == 0x8:
                    self._send_frame(client, 0x8, payload[:2])
                    break
                elif opcode in (0x1, 0x0):
                    parts.append(payload)
                    if fin:
                        text, parts = b''.join(parts).decode(), []
                        self._handle(client, json.loads(text))
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                if client in self.clients:
                    self.clients.remove(client)
            sock.close()

    def _send_frame(self, client, opcode, payload):
        with client[1]:
            client[0].sendall(server_frame(opcode, payload))

    def _send(self, client, message):
        data = json.dumps(message).encode()
        try:
            if self.fragment_messages and len(data) >= 3:
                third = len(data) // 3
                with client[1]:
                    client[0].sendall(server_frame(0x1, data[:third], fin=False) +
                                      server_frame(0x9, b'hi') +  # a ping between the fragments
                                      server_frame(0x0, data[third:2 * third], fin=False) +
                                      server_frame(0x0, data[2 * third:]))
            else:
                self._send_frame(client, 0x1, data)
        except OSError:
            pass

    def _volume_payload(self):
        if self.payload_format == 'old':
            scenario = {'external_arc': 'mastervolume_ext_speaker_arc',
                        'tv_speaker': 'mastervolume_tv_speaker'}.get(
                            self.sound_output, 'mastervolume_ext_speaker_optical')
            return {'returnValue': True, 'subscribed': True, 'scenario': scenario,
                    'volume': self.volume, 'muted': self.muted, 'action': 'changed'}
        return {'returnValue': True, 'subscribed': True, 'callerId': 'com.webos.service.apiadapter',
                'volumeStatus': {'activeStatus': True, 'adjustVolume': True, 'maxVolume': 100,
                                 'muteStatus': self.muted, 'volume': self.volume, 'mode': 'normal',
                                 'soundOutput': self.sound_output}}

    def _report(self):
        """Tell every subscriber the volume, as the TV does on any change."""
        with self.lock:
            payload = self._volume_payload()
            targets = [(c, sub_id) for c in self.clients for sub_id in c[2]]
        for client, sub_id in targets:
            self._send(client, {'type': 'response', 'id': sub_id, 'payload': payload})

    def _handle(self, client, message):
        with self.lock:
            self.received.append(message)
        kind, msg_id = message.get('type'), message.get('id')
        payload = message.get('payload') or {}
        if kind == 'register':
            self._register(client, msg_id, payload.get('client-key'))
            return
        if not client[3]:
            self._send(client, {'type': 'error', 'id': msg_id, 'error': '401 insufficient permissions',
                                'payload': {}})
            return
        uri = message.get('uri')
        if kind == 'subscribe' and uri == 'ssap://audio/getVolume':
            with self.lock:
                client[2].add(msg_id)
                answer = self._volume_payload()
            self._send(client, {'type': 'response', 'id': msg_id, 'payload': answer})
        elif kind == 'request' and uri == 'ssap://audio/setVolume':
            with self.lock:
                self.volume = payload['volume']
            self._send(client, {'type': 'response', 'id': msg_id, 'payload': {'returnValue': True}})
            self._report()
        elif kind == 'request' and uri == 'ssap://audio/setMute':
            with self.lock:
                self.muted = payload['mute']
            self._send(client, {'type': 'response', 'id': msg_id, 'payload': {'returnValue': True}})
            self._report()
        elif kind == 'request' and uri == 'ssap://system.notifications/createToast':
            with self.lock:
                self.toasts.append(payload.get('message'))
            self._send(client, {'type': 'response', 'id': msg_id, 'payload': {'returnValue': True}})

    def _register(self, client, msg_id, key):
        with self.lock:
            known = key in self.paired_keys
        if known:
            client[3] = True
            self._send(client, {'type': 'registered', 'id': msg_id, 'payload': {'client-key': key}})
            return
        with self.lock:
            self.prompts += 1
        self._send(client, {'type': 'response', 'id': msg_id,
                            'payload': {'pairingType': 'PROMPT', 'returnValue': True}})
        if self.on_prompt == 'accept':
            with self.lock:
                self.issued += 1
                key = f'fake-key-{self.issued}'
                self.paired_keys.add(key)
            client[3] = True
            self._send(client, {'type': 'registered', 'id': msg_id, 'payload': {'client-key': key}})
        elif self.on_prompt == 'reject':
            self._send(client, {'type': 'error', 'id': msg_id, 'error': '403 User denied access',
                                'payload': {}})


def temporary_cert(test):
    """A self-signed certificate for the test, or skip it if openssl isn't installed."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    pair = make_self_signed_cert(tmp.name)
    if pair is None:
        test.skipTest("openssl isn't available")
    return pair
