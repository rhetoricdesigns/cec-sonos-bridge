#!/usr/bin/env python3
"""
Tests for LG mode (following an LG TV's own volume over Wi-Fi) - run with:
    python3 -m unittest discover tests

The TV is a fake one (tests/fake_lg_tv.py): a real WebSocket server on this
machine.  The Sonos speaker is a fake soco module.
"""

import http.client
import json
import logging
import logging.handlers  # before FileHandler is patched below: its classes subclass it
import os
import queue
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Both modules log to /var/log at import time, which only exists on the Pi
with mock.patch('logging.FileHandler', lambda *a, **k: logging.NullHandler()):
    import cec_bridge
    import web_server

from fake_lg_tv import FakeLGTV, read_client_frame, server_frame, temporary_cert

logging.disable(logging.CRITICAL)

SPEAKER_IP = '192.168.1.50'


class FakeSpeaker:
    """soco.SoCo stand-in: remembers every volume and mute it was given."""

    def __init__(self, volume=30, mute=False):
        self._volume, self._mute = volume, mute
        self.volume_sets, self.mute_sets = [], []
        self.reads = 0

    @property
    def volume(self):
        self.reads += 1
        return self._volume

    @volume.setter
    def volume(self, value):
        self.volume_sets.append(value)
        self._volume = value

    @property
    def mute(self):
        return self._mute

    @mute.setter
    def mute(self, value):
        self.mute_sets.append(value)
        self._mute = value


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def closed_port():
    """A local port nothing listens on, so connecting to it is refused."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class ConfigTestCase(unittest.TestCase):
    """A temporary config.json, as the setup wizard leaves it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_file = os.path.join(tmp.name, 'config.json')
        self.write_config({'speaker_ip': SPEAKER_IP, 'speaker_name': 'Living Room', 'hdmi_port': '2'})
        for p in (mock.patch.object(cec_bridge, 'CONFIG_FILE', self.config_file),
                  mock.patch.object(web_server, 'CONFIG_FILE', self.config_file),
                  mock.patch.object(web_server, 'APP_DIR', tmp.name)):
            p.start()
            self.addCleanup(p.stop)

    def write_config(self, config):
        with open(self.config_file, 'w') as f:
            json.dump(config, f)

    def read_config(self):
        with open(self.config_file) as f:
            return json.load(f)


class FollowerTestCase(ConfigTestCase):
    """A follower thread, a fake TV, a fake speaker and a Sonos worker, all for real."""

    tv_options = {}

    def setUp(self):
        super().setUp()
        self.speaker = FakeSpeaker(volume=30)
        self.sonos_queue = queue.Queue()
        self.worker_paused = threading.Event()
        self.worker_paused.set()  # set = running
        patches = [
            mock.patch.dict(sys.modules, {'soco': types.SimpleNamespace(SoCo=lambda ip: self.speaker)}),
            mock.patch.object(cec_bridge, 'sonos_queue', self.sonos_queue),
            mock.patch.object(cec_bridge, 'current_volume', 30),
            mock.patch.object(cec_bridge, 'is_muted', False),
            mock.patch.object(cec_bridge, 'cec_dev', None),
            mock.patch.object(cec_bridge, 'cec_proc', None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.jobs_run = 0
        self.max_queued = 0
        self.worker_stop = False
        worker = threading.Thread(target=self.sonos_worker, daemon=True)
        worker.start()
        self.addCleanup(lambda: (setattr(self, 'worker_stop', True), worker.join(2)))
        self.tv = FakeLGTV(**dict({'paired_keys': {'k1'}}, **self.tv_options)).start()
        self.addCleanup(self.tv.stop)

    def sonos_worker(self):
        """What cec_bridge.sonos_worker does, but stoppable and pausable."""
        while not self.worker_stop:
            self.worker_paused.wait()
            self.max_queued = max(self.max_queued, self.sonos_queue.qsize())
            try:
                action, args = self.sonos_queue.get(timeout=0.02)
            except queue.Empty:
                continue
            action(*args)
            self.jobs_run += 1

    def make_follower(self, enabled=True, host='127.0.0.1', key='k1', secure=False, ports=None):
        self.write_config(dict(self.read_config(), lg_tv={
            'enabled': enabled, 'host': host, 'name': 'Fake LG', 'client_key': key, 'secure': secure}))
        follower = cec_bridge.LGFollower(SPEAKER_IP, ports=ports or {True: closed_port(),
                                                                     False: self.tv.port})
        follower.RETRY_FIRST, follower.RETRY_MAX = 0.05, 0.2
        follower.READ_TIMEOUT = 0.05
        follower.SETTLE_TIME = 0.1
        self.addCleanup(follower.stop)
        return follower

    def start_following(self, **kwargs):
        follower = self.make_follower(**kwargs)
        follower.start()
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'connected'),
                        follower.status())
        # connected, synced, subscribed
        self.assertTrue(self.tv.wait_until(lambda: self.tv.requests('ssap://audio/getVolume') or
                                           any(m.get('type') == 'subscribe' for m in self.tv.received)))
        self.assertTrue(wait_for(lambda: follower.status()['tv_volume'] == self.speaker._volume))
        return follower


# ---------------------------------------------------------------- WebSocket

class TestWebSocketFrames(unittest.TestCase):
    def test_client_frames_are_masked_with_every_length_encoding(self):
        for length, header in ((125, 2), (126, 4), (65536, 10)):
            payload = bytes(range(256)) * (length // 256) + bytes(length % 256)
            data = cec_bridge.encode_ws_frame(cec_bridge.WS_TEXT, payload)
            self.assertTrue(data[1] & 0x80, "client frames must be masked")
            self.assertEqual(len(data), header + 4 + length)
            self.assertNotEqual(data[header + 4:], payload, "payload must be masked on the wire")
            fin, opcode, decoded, size = cec_bridge.decode_ws_frame(bytearray(data))
            self.assertEqual((fin, opcode, decoded, size), (True, 1, payload, len(data)))

    def test_masks_are_random(self):
        a = cec_bridge.encode_ws_frame(1, b'hello')
        b = cec_bridge.encode_ws_frame(1, b'hello')
        self.assertNotEqual(a[2:6], b[2:6])

    def test_server_frames_of_every_length_decode(self):
        for length in (0, 125, 126, 65535, 65536):
            data = server_frame(1, b'x' * length)
            self.assertEqual(cec_bridge.decode_ws_frame(bytearray(data))[2], b'x' * length)
            self.assertIsNone(cec_bridge.decode_ws_frame(bytearray(data[:-1])) if length else None)

    def test_partial_header_waits_for_more(self):
        data = server_frame(1, b'x' * 70000)
        for cut in (1, 3, 9):
            self.assertIsNone(cec_bridge.decode_ws_frame(bytearray(data[:cut])))

    def test_huge_frame_is_refused(self):
        with self.assertRaises(cec_bridge.WebSocketError):
            cec_bridge.decode_ws_frame(bytearray(b'\x81\x7f' + struct.pack('!Q', 1 << 40)))

    def test_accept_key_matches_the_rfc_example(self):
        self.assertEqual(cec_bridge.websocket_accept_key('dGhlIHNhbXBsZSBub25jZQ=='),
                         's3pPLMBiTxaQ9kYGzzhZRbK+xOo=')


class TestWebSocketConnection(unittest.TestCase):
    """The client on one end of a socket pair; the test plays the server."""

    def setUp(self):
        client, self.server = socket.socketpair()
        self.addCleanup(self.server.close)
        self.ws = cec_bridge.WebSocket(client)
        self.addCleanup(self.ws.close)
        self.buffer = bytearray()

    def from_client(self):
        self.server.settimeout(2)
        return read_client_frame(self.server, self.buffer)

    def test_text_message(self):
        self.server.sendall(server_frame(1, '{"a": 1}'.encode()))
        self.assertEqual(self.ws.receive(1), '{"a": 1}')

    def test_sends_masked_text(self):
        self.ws.send_text('hi')
        self.assertEqual(self.from_client(), (True, 1, b'hi', True))

    def test_answers_ping_with_pong(self):
        self.server.sendall(server_frame(9, b'abc'))
        self.assertIsNone(self.ws.receive(0.1))
        self.assertEqual(self.from_client(), (True, 0xA, b'abc', True))

    def test_notes_pong(self):
        before = self.ws.last_pong
        time.sleep(0.01)
        self.server.sendall(server_frame(0xA, b''))
        self.ws.receive(0.1)
        self.assertGreater(self.ws.last_pong, before)

    def test_answers_close_and_reports_it(self):
        self.server.sendall(server_frame(8, struct.pack('!H', 1000)))
        with self.assertRaises(cec_bridge.WebSocketClosed):
            self.ws.receive(1)
        self.assertEqual(self.from_client(), (True, 8, struct.pack('!H', 1000), True))

    def test_reassembles_fragments_with_a_ping_between(self):
        self.server.sendall(server_frame(1, b'{"a"', fin=False) + server_frame(9, b'') +
                            server_frame(0, b': ', fin=False) + server_frame(0, b'2}'))
        self.assertEqual(self.ws.receive(1), '{"a": 2}')
        self.assertEqual(self.from_client()[1], 0xA)

    def test_ignores_binary(self):
        self.server.sendall(server_frame(2, b'\x00\x01') + server_frame(1, b'text'))
        self.assertEqual(self.ws.receive(1), 'text')

    def test_message_split_across_reads(self):
        data = server_frame(1, b'y' * 300)
        self.server.sendall(data[:3])
        self.assertIsNone(self.ws.receive(0.05))
        self.server.sendall(data[3:])
        self.assertEqual(self.ws.receive(1), 'y' * 300)

    def test_connection_end_is_reported(self):
        self.server.close()
        with self.assertRaises(cec_bridge.WebSocketClosed):
            self.ws.receive(1)

    def test_timeout_returns_none_quickly(self):
        start = time.monotonic()
        self.assertIsNone(self.ws.receive(0.05))
        self.assertLess(time.monotonic() - start, 0.5)

    def test_interrupt_returns_at_once(self):
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        writer.send(b'.')
        start = time.monotonic()
        self.assertIsNone(self.ws.receive(5, interrupt=reader))
        self.assertLess(time.monotonic() - start, 1)


class TestWebSocketHandshake(unittest.TestCase):
    def test_handshake_with_the_fake_tv(self):
        tv = FakeLGTV().start()
        self.addCleanup(tv.stop)
        ws = cec_bridge.WebSocket.connect('127.0.0.1', tv.port, secure=False, timeout=2)
        ws.close()

    def test_wrong_accept_is_rejected(self):
        tv = FakeLGTV().start()
        tv.bad_accept = True
        self.addCleanup(tv.stop)
        with self.assertRaises(cec_bridge.WebSocketError):
            cec_bridge.WebSocket.connect('127.0.0.1', tv.port, secure=False, timeout=2)

    def test_tls_with_a_self_signed_certificate(self):
        tv = FakeLGTV(tls=temporary_cert(self)).start()
        self.addCleanup(tv.stop)
        ws = cec_bridge.WebSocket.connect('127.0.0.1', tv.port, secure=True, timeout=5)
        conn = cec_bridge.LGTVConnection(ws)
        self.assertEqual(conn.register('', allow_prompt=True, timeout=2), 'fake-key-1')
        conn.close()


# ---------------------------------------------------------------- SSAP and parsing

class TestVolumeParsing(unittest.TestCase):
    def test_new_webos(self):
        payload = {'volumeStatus': {'volume': 12, 'muteStatus': True, 'soundOutput': 'external_optical',
                                    'adjustVolume': True, 'maxVolume': 100}, 'returnValue': True}
        self.assertEqual(cec_bridge.parse_lg_volume(payload), (12, True, 'external_optical'))

    def test_old_webos(self):
        payload = {'volume': 12, 'muted': False, 'scenario': 'mastervolume_tv_speaker'}
        self.assertEqual(cec_bridge.parse_lg_volume(payload), (12, False, 'mastervolume_tv_speaker'))

    def test_old_webos_with_mute_key(self):
        self.assertEqual(cec_bridge.parse_lg_volume({'volume': 5, 'mute': True}), (5, True, None))

    def test_missing_and_odd_values(self):
        self.assertEqual(cec_bridge.parse_lg_volume({}), (None, None, None))
        self.assertEqual(cec_bridge.parse_lg_volume(None), (None, None, None))
        self.assertEqual(cec_bridge.parse_lg_volume({'volume': True}), (None, None, None))
        self.assertEqual(cec_bridge.parse_lg_volume({'volume': -1})[0], -1)

    def test_sound_output_names(self):
        describe = cec_bridge.describe_lg_sound_output
        self.assertEqual(describe('external_optical'), 'Optical')
        self.assertEqual(describe('external_arc'), 'HDMI ARC')
        self.assertEqual(describe('mastervolume_ext_speaker_arc'), 'HDMI ARC')
        self.assertEqual(describe('tv_speaker'), 'TV speakers')
        self.assertEqual(describe('mastervolume_tv_speaker'), 'TV speakers')
        self.assertEqual(describe('bt_soundbar'), 'Bluetooth')
        self.assertEqual(describe('something_new'), 'something_new')
        self.assertIsNone(describe(None))

    def test_hints(self):
        hint = cec_bridge.lg_sound_output_hint
        self.assertEqual(hint(-1, None), cec_bridge.LG_HINT_ARC)
        self.assertEqual(hint(20, 'external_arc'), cec_bridge.LG_HINT_ARC)
        self.assertEqual(hint(20, 'tv_speaker'), cec_bridge.LG_HINT_SPEAKERS)
        self.assertIsNone(hint(20, 'external_optical'))

    def test_registration_manifest_is_aiowebostvs(self):
        payload = cec_bridge.LG_REGISTRATION_PAYLOAD
        self.assertEqual(payload['pairingType'], 'PROMPT')
        self.assertFalse(payload['forcePairing'])
        self.assertIn('CONTROL_AUDIO', payload['manifest']['permissions'])
        self.assertIn('WRITE_NOTIFICATION_TOAST', payload['manifest']['permissions'])
        self.assertEqual(len(payload['manifest']['permissions']), 37)


class TestRegistration(unittest.TestCase):
    def connect(self, tv):
        tv.start()
        self.addCleanup(tv.stop)
        return cec_bridge.LGTVConnection(cec_bridge.WebSocket.connect('127.0.0.1', tv.port, False, 2))

    def test_already_paired(self):
        tv = FakeLGTV(paired_keys={'k1'})
        conn = self.connect(tv)
        self.assertEqual(conn.register('k1', allow_prompt=False, timeout=2), 'k1')
        self.assertEqual(tv.prompts, 0)
        self.assertEqual(tv.received[0]['payload']['client-key'], 'k1')
        conn.close()

    def test_no_key_is_left_out(self):
        tv = FakeLGTV()
        conn = self.connect(tv)
        conn.register('', allow_prompt=True, timeout=2)
        self.assertNotIn('client-key', tv.received[0]['payload'])
        self.assertEqual(tv.received[0]['type'], 'register')
        self.assertEqual(tv.received[0]['id'], 'register_0')
        conn.close()

    def test_prompt_then_accept(self):
        tv = FakeLGTV(on_prompt='accept')
        conn = self.connect(tv)
        prompted = []
        self.assertEqual(conn.register('', allow_prompt=True, timeout=2,
                                       on_prompt=lambda: prompted.append(1)), 'fake-key-1')
        self.assertEqual(prompted, [1])
        conn.close()

    def test_prompt_then_reject(self):
        conn = self.connect(FakeLGTV(on_prompt='reject'))
        with self.assertRaises(cec_bridge.LGPairingError) as caught:
            conn.register('', allow_prompt=True, timeout=2)
        self.assertEqual(caught.exception.reason, 'refused')
        conn.close()

    def test_prompt_unanswered(self):
        conn = self.connect(FakeLGTV(on_prompt='ignore'))
        with self.assertRaises(cec_bridge.LGPairingError) as caught:
            conn.register('', allow_prompt=True, timeout=2, prompt_timeout=0.2)
        self.assertEqual(caught.exception.reason, 'timeout')
        conn.close()

    def test_prompt_not_wanted(self):
        conn = self.connect(FakeLGTV(on_prompt='accept'))
        with self.assertRaises(cec_bridge.LGPairingError) as caught:
            conn.register('old-key', allow_prompt=False, timeout=2)
        self.assertEqual(caught.exception.reason, 'not_paired')
        conn.close()


# ---------------------------------------------------------------- discovery

SSDP_REPLY = (b'HTTP/1.1 200 OK\r\n'
              b'CACHE-CONTROL: max-age=1800\r\n'
              b'EXT:\r\n'
              b'LOCATION: http://192.168.1.40:1999/\r\n'
              b'SERVER: WebOS/4.1.0 UPnP/1.0 webOSTV/1.0\r\n'
              b'ST: urn:lge-com:service:webos-second-screen:1\r\n'
              b'USN: uuid:4b8e6f5a-1234-5678-9abc-def012345678::urn:lge-com:service:webos-second-screen:1\r\n'
              b'\r\n')

SONOS_REPLY = (b'HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n'
               b'USN: uuid:RINCON_1::urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n')


class FakeUDPSocket:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.closed = False

    def sendto(self, data, address):
        self.sent.append((data, address))

    def settimeout(self, timeout):
        pass

    def recvfrom(self, size):
        if not self.replies:
            raise socket.timeout()
        return self.replies.pop(0)

    def close(self):
        self.closed = True


class TestDiscovery(unittest.TestCase):
    def test_parses_an_lg_reply(self):
        tv = cec_bridge.parse_ssdp_reply(SSDP_REPLY, ('192.168.1.40', 1900))
        self.assertEqual(tv, {'host': '192.168.1.40', 'name': '', 'location': 'http://192.168.1.40:1999/',
                              'uuid': '4b8e6f5a-1234-5678-9abc-def012345678'})

    def test_ignores_other_devices(self):
        self.assertIsNone(cec_bridge.parse_ssdp_reply(SONOS_REPLY, ('192.168.1.50', 1900)))
        self.assertIsNone(cec_bridge.parse_ssdp_reply(b'garbage', ('192.168.1.50', 1900)))

    def test_search_collects_each_tv_once(self):
        sock = FakeUDPSocket([(SSDP_REPLY, ('192.168.1.40', 1900)), (SONOS_REPLY, ('192.168.1.50', 1900)),
                              (SSDP_REPLY, ('192.168.1.40', 1900))])
        tvs = cec_bridge.discover_lg_tvs(timeout=1, fetch_names=False, sock=sock)
        self.assertEqual([tv['host'] for tv in tvs], ['192.168.1.40'])
        request, address = sock.sent[0]
        self.assertEqual(address, ('239.255.255.250', 1900))
        self.assertIn(b'ST: urn:lge-com:service:webos-second-screen:1\r\n', request)
        self.assertIn(b'MAN: "ssdp:discover"\r\n', request)
        self.assertIn(b'MX: 2\r\n', request)
        self.assertTrue(sock.closed)

    def test_friendly_name_only_from_the_tv_itself(self):
        with mock.patch.object(cec_bridge.urllib.request, 'urlopen') as urlopen:
            self.assertEqual(cec_bridge.fetch_lg_tv_name('http://10.0.0.9/desc.xml', '192.168.1.40'), '')
            urlopen.assert_not_called()

    def test_friendly_name(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'<root><device><friendlyName>[LG] webOS TV OLED55C1</friendlyName></device></root>')
        with mock.patch.object(cec_bridge.urllib.request, 'urlopen', return_value=response):
            self.assertEqual(cec_bridge.fetch_lg_tv_name('http://192.168.1.40:1999/', '192.168.1.40'),
                             '[LG] webOS TV OLED55C1')


# ---------------------------------------------------------------- config

class TestConfig(ConfigTestCase):
    def test_defaults_without_lg_settings(self):
        self.assertEqual(cec_bridge.load_lg_settings(), cec_bridge.LG_TV_DEFAULTS)

    def test_save_merges_and_keeps_other_settings(self):
        cec_bridge.save_lg_settings(host='192.168.1.40', client_key='abc')
        cec_bridge.save_lg_settings(enabled=True)
        config = self.read_config()
        self.assertEqual(config['speaker_ip'], SPEAKER_IP)
        self.assertEqual(config['speaker_name'], 'Living Room')
        self.assertEqual(config['lg_tv'], {'enabled': True, 'host': '192.168.1.40', 'name': '',
                                           'client_key': 'abc', 'secure': None})

    def test_save_is_atomic(self):
        with mock.patch.object(cec_bridge.os, 'replace', wraps=os.replace) as replace:
            cec_bridge.save_lg_settings(enabled=True)
        replace.assert_called_once_with(self.config_file + '.tmp', self.config_file)

    def test_failed_write_leaves_the_config_alone(self):
        before = self.read_config()
        with mock.patch.object(cec_bridge.json, 'dump', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                cec_bridge.save_lg_settings(enabled=True)
        self.assertEqual(self.read_config(), before)

    def test_damaged_config_is_not_overwritten(self):
        with open(self.config_file, 'w') as f:
            f.write('{not json')
        with self.assertRaises(ValueError):
            cec_bridge.save_lg_settings(enabled=True)
        with open(self.config_file) as f:
            self.assertEqual(f.read(), '{not json')

    def test_web_server_save_keeps_lg_settings(self):
        config = web_server.get_config()                  # the settings page reads...
        cec_bridge.save_lg_settings(client_key='paired')  # ...the follower pairs meanwhile...
        config['hdmi_port'] = '3'
        web_server.save_config(config)                    # ...and the settings page saves
        saved = self.read_config()
        self.assertEqual(saved['hdmi_port'], '3')
        self.assertEqual(saved['lg_tv']['client_key'], 'paired')

    def test_valid_hosts(self):
        for host in ('192.168.1.40', 'lgwebostv.local', 'tv-1'):
            self.assertTrue(cec_bridge.is_valid_tv_host(host), host)
        for host in ('', ' ', '1.2.3.4\r\nX: y', 'a/b', '-x', 'a' * 300):
            self.assertFalse(cec_bridge.is_valid_tv_host(host), host)


# ---------------------------------------------------------------- the follower

class TestPairing(FollowerTestCase):
    def pair(self, follower, host='127.0.0.1', name='Fake LG'):
        follower.start()
        follower.request_pairing(host, name)
        self.assertTrue(wait_for(lambda: follower.status()['pairing']['state'] in ('paired', 'failed')),
                        follower.status())
        return follower.status()['pairing']

    def test_pair_saves_the_key_and_keeps_other_settings(self):
        follower = self.make_follower(enabled=False, host='', key='')
        self.tv.paired_keys = set()
        pairing = self.pair(follower)
        self.assertEqual(pairing, {'state': 'paired', 'message': 'Paired with Fake LG.'})
        config = self.read_config()
        self.assertEqual(config['speaker_ip'], SPEAKER_IP)
        self.assertEqual(config['lg_tv']['client_key'], 'fake-key-1')
        self.assertEqual(config['lg_tv']['host'], '127.0.0.1')
        self.assertEqual(config['lg_tv']['name'], 'Fake LG')
        self.assertIs(config['lg_tv']['secure'], False)  # wss refused, ws worked
        self.assertTrue(follower.status()['paired'])
        self.assertTrue(self.tv.wait_until(lambda: self.tv.toasts == ['Sonos Bridge connected']))
        self.assertEqual(self.tv.prompts, 1)

    def test_pair_then_follow(self):
        follower = self.make_follower(enabled=True, host='', key='')
        self.tv.paired_keys = set()
        self.pair(follower)
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'connected'), follower.status())

    def test_refused_pairing(self):
        self.tv.on_prompt = 'reject'
        follower = self.make_follower(enabled=False, host='', key='')
        pairing = self.pair(follower)
        self.assertEqual(pairing['state'], 'failed')
        self.assertIn('said no', pairing['message'])
        self.assertEqual(self.read_config()['lg_tv']['client_key'], '')
        self.assertNotIn(cec_bridge.LG_HINT_CONNECT_APPS, follower.status()['hints'])
        # A second failure suggests turning on LG Connect Apps
        follower.request_pairing('127.0.0.1')
        self.assertTrue(wait_for(lambda: cec_bridge.LG_HINT_CONNECT_APPS in follower.status()['hints']))

    def test_unreachable_tv(self):
        self.tv.stop()
        follower = self.make_follower(enabled=False, host='', key='')
        pairing = self.pair(follower)
        self.assertEqual(pairing['state'], 'failed')
        self.assertIn('refused the connection', pairing['message'])

    def test_bad_host_is_refused(self):
        follower = self.make_follower(enabled=False)
        with self.assertRaises(ValueError):
            follower.request_pairing('1.2.3.4\r\nHost: evil')

    def test_unasked_prompt_is_not_repeated(self):
        """The TV forgot the bridge: it asks once, then the bridge waits for the owner to press Pair."""
        self.tv.paired_keys = set()
        self.tv.on_prompt = 'ignore'
        follower = self.make_follower(key='forgotten')
        follower.start()
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'not_paired'), follower.status())
        time.sleep(0.5)  # many retry periods
        self.assertEqual(self.tv.prompts, 1)
        self.assertEqual(self.speaker.volume_sets, [])

    def test_forget(self):
        follower = self.start_following()
        follower.forget()
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'not_paired'))
        lg = self.read_config()['lg_tv']
        self.assertEqual((lg['host'], lg['client_key'], lg['enabled']), ('', '', True))
        self.assertTrue(self.tv.wait_until(lambda: self.tv.connected() == 0))


class TestFollowing(FollowerTestCase):
    def test_connect_sets_the_tv_to_the_speaker(self):
        self.speaker._volume = 37
        follower = self.start_following()
        self.assertEqual(self.tv.volume, 37)
        self.assertEqual(self.speaker.volume_sets, [])  # the speaker didn't jump to the TV's 10
        self.assertEqual(self.speaker.mute_sets, [])
        self.assertEqual(self.read_config()['lg_tv']['secure'], False)
        status = follower.status()
        self.assertEqual((status['tv_volume'], status['sonos_volume']), (37, 37))
        self.assertEqual(status['sound_output'], 'Optical')
        self.assertEqual(status['hints'], [])

    def test_volume_follows_the_tv(self):
        follower = self.start_following()
        self.tv.press_volume(24)
        self.assertTrue(wait_for(lambda: self.speaker._volume == 24))
        self.assertEqual(self.speaker.volume_sets, [24])
        self.assertTrue(wait_for(lambda: follower.status()['sonos_volume'] == 24))
        self.assertIsNotNone(follower.status()['last_change_ago'])

    def test_mute_follows_the_tv(self):
        self.start_following()
        self.tv.set_mute(True)
        self.assertTrue(wait_for(lambda: self.speaker._mute is True))
        self.tv.set_mute(False)
        self.assertTrue(wait_for(lambda: self.speaker._mute is False))
        self.assertEqual(self.speaker.mute_sets, [True, False])
        self.assertEqual(self.speaker.volume_sets, [])

    def test_no_op_reports_leave_the_speaker_alone(self):
        self.start_following()
        jobs = self.jobs_run
        self.tv.press_volume(30)  # what the speaker already has
        self.tv.report(sound_output='external_optical')
        time.sleep(0.2)
        self.assertEqual(self.speaker.volume_sets, [])
        self.assertEqual(self.jobs_run, jobs)

    def test_minus_one_is_ignored_with_a_hint(self):
        follower = self.start_following()
        self.tv.report(volume=-1, sound_output='external_arc')
        self.assertTrue(wait_for(lambda: follower.status()['tv_volume'] == -1))
        self.assertEqual(follower.status()['hints'], [cec_bridge.LG_HINT_ARC])
        self.assertEqual(follower.status()['sound_output'], 'HDMI ARC')
        self.assertEqual(self.speaker.volume_sets, [])

    def test_tv_speakers_hint(self):
        follower = self.start_following()
        self.tv.report(sound_output='tv_speaker')
        self.assertTrue(wait_for(lambda: follower.status()['hints'] == [cec_bridge.LG_HINT_SPEAKERS]))

    def test_burst_of_presses_is_collapsed(self):
        self.start_following()
        self.worker_paused.clear()  # a slow speaker
        time.sleep(0.05)
        for volume in range(31, 51):
            self.tv.press_volume(volume)
        self.assertTrue(wait_for(lambda: self.sonos_queue.qsize() >= 1))
        time.sleep(0.2)
        self.assertEqual(self.sonos_queue.qsize(), 1)  # never more than one job waiting
        self.worker_paused.set()
        self.assertTrue(wait_for(lambda: self.speaker._volume == 50))
        self.assertLessEqual(len(self.speaker.volume_sets), 3)
        self.assertEqual(self.speaker.volume_sets[-1], 50)

    def test_settled_change_is_logged_once(self):
        follower = self.start_following()
        with mock.patch.object(cec_bridge, 'log') as log:
            for volume in (31, 32, 33):
                self.tv.press_volume(volume)
            self.assertTrue(wait_for(lambda: any('LG TV volume 33 -> Sonos 33' in str(c)
                                                 for c in log.info.call_args_list)))
            time.sleep(0.2)
        lines = [str(c) for c in log.info.call_args_list if 'LG TV volume' in str(c)]
        self.assertEqual(len(lines), 1, lines)
        self.assertIsNotNone(follower)

    def test_cec_volume_key_is_pushed_to_the_tv(self):
        self.start_following()
        with mock.patch.object(cec_bridge, 'lg_follower', self.follower_from_cleanup()):
            cec_bridge.handle_volume(SPEAKER_IP, 'up')
            self.assertTrue(self.tv.wait_until(lambda: self.tv.volume == 32))
            cec_bridge.handle_volume(SPEAKER_IP, 'up')
            cec_bridge.handle_volume(SPEAKER_IP, 'up')
            self.assertTrue(self.tv.wait_until(lambda: self.tv.volume == 36))
            time.sleep(0.2)  # the TV's echoes come back
        self.assertEqual(self.speaker._volume, 36)
        self.assertEqual(self.speaker.volume_sets, [32, 34, 36])  # no echo pulled it back

    def test_cec_mute_key_is_pushed_to_the_tv(self):
        self.start_following()
        with mock.patch.object(cec_bridge, 'lg_follower', self.follower_from_cleanup()):
            cec_bridge.handle_mute(SPEAKER_IP)
            self.assertTrue(self.tv.wait_until(lambda: self.tv.muted is True))
            time.sleep(0.2)
        self.assertEqual(self.speaker.mute_sets, [True])

    def follower_from_cleanup(self):
        """The follower start_following made (the last one created)."""
        return self._follower

    def make_follower(self, **kwargs):
        self._follower = super().make_follower(**kwargs)
        return self._follower

    def test_old_webos_payloads(self):
        self.tv.payload_format = 'old'
        follower = self.start_following()
        self.tv.press_volume(22)
        self.assertTrue(wait_for(lambda: self.speaker._volume == 22))
        self.tv.set_mute(True)
        self.assertTrue(wait_for(lambda: self.speaker._mute is True))
        self.tv.report(volume=-1, sound_output='external_arc')
        self.assertTrue(wait_for(lambda: follower.status()['hints'] == [cec_bridge.LG_HINT_ARC]))
        self.assertEqual(self.speaker.volume_sets, [22])

    def test_fragmented_messages(self):
        self.tv.fragment_messages = True
        self.start_following()
        self.tv.press_volume(26)
        self.assertTrue(wait_for(lambda: self.speaker._volume == 26))

    def test_reconnects_after_the_tv_turns_off(self):
        follower = self.start_following()
        self.tv.power_off()
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'unreachable'), follower.status())
        self.assertFalse(follower.status()['connected'])
        self.tv.power_on()
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'connected'), follower.status())
        self.tv.press_volume(41)
        self.assertTrue(wait_for(lambda: self.speaker._volume == 41))

    def test_missing_pong_counts_as_gone(self):
        follower = self.make_follower()
        follower.PING_INTERVAL, follower.PONG_TIMEOUT = 0.1, 0.2
        self.tv.answer_pings = False
        follower.start()
        self.assertTrue(wait_for(lambda: self.tv.connections >= 2), "should have reconnected")
        self.tv.answer_pings = True

    def test_answered_pings_keep_the_connection(self):
        follower = self.make_follower()
        follower.PING_INTERVAL, follower.PONG_TIMEOUT = 0.05, 0.3
        follower.start()
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'connected'))
        time.sleep(0.6)
        self.assertEqual(self.tv.connections, 1)

    def test_stops_cleanly(self):
        follower = self.start_following()
        start = time.monotonic()
        follower.stop()
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertFalse(follower.thread.is_alive())
        self.assertTrue(self.tv.wait_until(lambda: self.tv.connected() == 0))

    def test_disable_disconnects(self):
        follower = self.start_following()
        follower.set_enabled(False)
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'off'))
        self.assertTrue(self.tv.wait_until(lambda: self.tv.connected() == 0))
        self.assertFalse(self.read_config()['lg_tv']['enabled'])
        self.tv.press_volume(12)
        time.sleep(0.1)
        self.assertEqual(self.speaker.volume_sets, [])

    def test_secure_connection_first(self):
        cert = temporary_cert(self)
        tls_tv = FakeLGTV(paired_keys={'k1'}, tls=cert).start()
        self.addCleanup(tls_tv.stop)
        follower = self.make_follower(secure=None, ports={True: tls_tv.port, False: closed_port()})
        follower.start()
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'connected', timeout=5))
        self.assertTrue(wait_for(lambda: self.read_config()['lg_tv']['secure'] is True))
        tls_tv.press_volume(19)
        self.assertTrue(wait_for(lambda: self.speaker._volume == 19))


class TestBackoff(FollowerTestCase):
    def test_retry_delays_double_up_to_the_maximum(self):
        self.tv.stop()
        follower = self.make_follower()
        follower.RETRY_FIRST, follower.RETRY_MAX = 0.01, 0.08
        delays = []
        real_wait = follower.changed.wait

        def wait(timeout=None):
            if timeout is not None:
                delays.append(timeout)
                if len(delays) >= 6:
                    follower.stopping = True
            return real_wait(0.001 if timeout is not None else 0.05)

        follower.changed.wait = wait
        follower.start()
        follower.thread.join(3)
        self.assertEqual(delays[:6], [0.01, 0.02, 0.04, 0.08, 0.08, 0.08])

    def test_retry_delay_resets_after_a_connection(self):
        follower = self.start_following()
        delays = []
        real_wait = follower.changed.wait
        follower.changed.wait = lambda timeout=None: (delays.append(timeout), real_wait(timeout))[1]
        self.tv.power_off()
        self.assertTrue(wait_for(lambda: len(delays) >= 3))
        self.tv.power_on()
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'connected'))
        self.tv.power_off()
        before = len(delays)
        self.assertTrue(wait_for(lambda: len(delays) > before))
        self.assertEqual(delays[0], follower.RETRY_FIRST)
        self.assertEqual(delays[before], follower.RETRY_FIRST)


class TestLGModeOff(FollowerTestCase):
    """With LG mode off - the default - the follower waits and touches nothing."""

    def test_off_by_default_does_nothing(self):
        follower = cec_bridge.LGFollower(SPEAKER_IP, ports={True: self.tv.port, False: self.tv.port})
        self.addCleanup(follower.stop)
        self.assertFalse(follower.settings['enabled'])
        follower.start()
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'off'))
        time.sleep(0.2)
        self.assertEqual(self.tv.connections, 0)
        self.assertEqual(self.jobs_run, 0)
        self.assertEqual(self.speaker.reads + len(self.speaker.volume_sets), 0)
        self.assertNotIn('lg_tv', self.read_config())  # nothing written either

    def test_turning_it_on_connects(self):
        follower = self.make_follower(enabled=False)
        follower.start()
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'off'))
        follower.set_enabled(True)
        self.assertTrue(wait_for(lambda: follower.status()['state'] == 'connected'))

    def test_cec_keys_without_a_follower(self):
        with mock.patch.object(cec_bridge, 'lg_follower', None):
            cec_bridge.handle_volume(SPEAKER_IP, 'up')
        self.assertEqual(self.speaker.volume_sets, [32])

    def test_cec_keys_while_not_connected(self):
        follower = self.make_follower(enabled=False)
        follower.start()
        with mock.patch.object(cec_bridge, 'lg_follower', follower):
            cec_bridge.handle_volume(SPEAKER_IP, 'down')
            cec_bridge.handle_mute(SPEAKER_IP)
        self.assertIsNone(follower.outbox)
        self.assertEqual(self.tv.connections, 0)
        self.assertEqual(self.speaker.volume_sets, [28])


class TestSonosSetters(FollowerTestCase):
    def test_exact_level(self):
        with mock.patch.object(cec_bridge, 'report_audio_status') as report:
            self.assertTrue(cec_bridge.set_sonos_level(SPEAKER_IP, 44, True))
            report.assert_called_once_with()
        self.assertEqual((self.speaker.volume_sets, self.speaker.mute_sets), ([44], [True]))
        self.assertEqual((cec_bridge.current_volume, cec_bridge.is_muted), (44, True))

    def test_only_what_differs(self):
        self.assertTrue(cec_bridge.set_sonos_level(SPEAKER_IP, 30, True))
        self.assertFalse(cec_bridge.set_sonos_level(SPEAKER_IP, 30, True))
        self.assertEqual((self.speaker.volume_sets, self.speaker.mute_sets), ([], [True]))

    def test_read_level(self):
        self.speaker._volume, self.speaker._mute = 12, True
        self.assertEqual(cec_bridge.read_sonos_level(SPEAKER_IP), (12, True))
        self.assertEqual((cec_bridge.current_volume, cec_bridge.is_muted), (12, True))


# ---------------------------------------------------------------- the admin panel

class StubFollower:
    def __init__(self):
        self.calls = []

    def status(self):
        return {'enabled': True, 'state': 'connected', 'message': 'Connected.', 'hints': []}

    def find_tvs(self):
        self.calls.append('find')
        return [{'host': '192.168.1.40', 'name': 'LG TV', 'location': 'x', 'uuid': 'y'}]

    def request_pairing(self, host, name=''):
        if not cec_bridge.is_valid_tv_host(host):
            raise ValueError(host)
        self.calls.append(('pair', host, name))

    def set_enabled(self, enabled):
        self.calls.append(('enable', enabled))

    def forget(self):
        self.calls.append('forget')


class TestWebEndpoints(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.server = HTTPServer(('127.0.0.1', 0), web_server.WebHandler)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={'Content-Type': 'application/json'})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data) if data else None

    def test_not_available_without_the_bridge(self):
        with mock.patch.object(web_server, 'lg_follower', None):
            self.assertEqual(self.call('GET', '/api/lg/status'),
                             (200, {'available': False, 'message': web_server.LG_UNAVAILABLE}))
            for path in ('/api/lg/find', '/api/lg/pair', '/api/lg/enable', '/api/lg/forget'):
                self.assertEqual(self.call('POST', path, {})[1],
                                 {'success': False, 'message': web_server.LG_UNAVAILABLE})

    def test_endpoints_drive_the_follower(self):
        stub = StubFollower()
        with mock.patch.object(web_server, 'lg_follower', stub):
            status, data = self.call('GET', '/api/lg/status')
            self.assertEqual(data['available'], True)
            self.assertEqual(data['state'], 'connected')
            self.assertEqual(self.call('POST', '/api/lg/find')[1],
                             {'success': True, 'tvs': [{'host': '192.168.1.40', 'name': 'LG TV'}]})
            self.assertEqual(self.call('POST', '/api/lg/pair', {'host': '192.168.1.40', 'name': 'LG TV'})[1],
                             {'success': True})
            self.assertFalse(self.call('POST', '/api/lg/pair', {'host': 'not a host!'})[1]['success'])
            self.assertEqual(self.call('POST', '/api/lg/enable', {'enabled': True})[1], {'success': True})
            self.assertEqual(self.call('POST', '/api/lg/enable', {'enabled': False})[1], {'success': True})
            self.assertEqual(self.call('POST', '/api/lg/forget')[1], {'success': True})
            self.assertEqual(self.call('POST', '/api/lg/nonsense')[0], 404)
        self.assertEqual(stub.calls, ['find', ('pair', '192.168.1.40', 'LG TV'), ('enable', True),
                                      ('enable', False), 'forget'])

    def test_real_follower_status_and_enable(self):
        follower = cec_bridge.LGFollower(SPEAKER_IP)  # thread not started: just the settings
        with mock.patch.object(web_server, 'lg_follower', follower):
            data = self.call('GET', '/api/lg/status')[1]
            self.assertEqual((data['available'], data['enabled'], data['paired'], data['hints']),
                             (True, False, False, []))
            self.call('POST', '/api/lg/enable', {'enabled': True})
            self.assertTrue(self.call('GET', '/api/lg/status')[1]['enabled'])
        self.assertTrue(self.read_config()['lg_tv']['enabled'])
        self.assertEqual(self.read_config()['speaker_ip'], SPEAKER_IP)

    def test_status_polls_are_not_logged(self):
        with mock.patch.object(web_server, 'lg_follower', None), \
                mock.patch.object(web_server, 'log') as log:
            self.call('GET', '/api/lg/status')
            self.call('GET', '/api/admin/backups')
        logged = ' '.join(str(c) for c in log.info.call_args_list)
        self.assertNotIn('/api/lg/status', logged)
        self.assertIn('/api/admin/backups', logged)

    def test_admin_page_has_the_lg_tab(self):
        page = web_server.ADMIN_PAGE_HTML
        for text in ('id="tabLg"', 'Follow LG TV volume', 'Find my TV', 'Pair with TV', 'Forget TV',
                     'TV via IR', 'Settings &gt; Sound &gt; Sound Out'):
            self.assertIn(text, page)
        self.assertNotIn('confirm(', page)


class TestShippedFiles(unittest.TestCase):
    def test_update_files_unchanged(self):
        self.assertEqual(web_server.UPDATE_FILES,
                         ['startup.py', 'ap_mode.py', 'cec_bridge.py', 'web_server.py', 'splash_screen.py'])

    def test_web_server_never_imports_cec_bridge(self):
        with open(web_server.__file__) as f:
            source = f.read()
        self.assertNotIn('import cec_bridge', source)
        self.assertNotIn('from cec_bridge', source)


if __name__ == '__main__':
    unittest.main()
