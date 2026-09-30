#!/usr/bin/env python3
"""
Tests for the CEC activity log (sonosbridge.local/cec) - run with:
    python3 -m unittest discover tests
"""

import errno
import logging
import logging.handlers  # before FileHandler is patched below: its classes subclass it
import os
import select
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Both modules log to /var/log at import time, which only exists on the Pi
with mock.patch('logging.FileHandler', lambda *a, **k: logging.NullHandler()):
    import cec_bridge
    import web_server

SPEAKER_IP = '192.168.1.50'


def frame(text):
    """'05:44:41' -> b'\\x05\\x44\\x41'"""
    return bytes(int(b, 16) for b in text.split(':'))


class ActivityTestCase(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(cec_bridge, 'traffic_log'),
            mock.patch.object(cec_bridge, 'log'),
            mock.patch.object(cec_bridge, 'bridge_phys_addr', 0x2000),  # bridge on HDMI 2
            mock.patch.object(cec_bridge, 'traffic_monitor_active', False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def logged(self):
        return [c.args[0] for c in cec_bridge.traffic_log.info.call_args_list]


class TestDescribeMessages(unittest.TestCase):
    def describe(self, text):
        return cec_bridge.describe_cec_frame(frame(text))

    def test_fire_tv_one_touch_play(self):
        self.assertEqual(self.describe('4F:82:10:00'), 'Player 1 -> all: Active Source 1.0.0.0')
        self.assertEqual(self.describe('40:0D'), 'Player 1 -> TV: Text View On')
        self.assertEqual(self.describe('45:70:10:00'),
                         'Player 1 -> Bridge: System Audio Mode Request for 1.0.0.0')

    def test_bridge_replies(self):
        self.assertEqual(self.describe('5F:72:01'), 'Bridge -> all: Set System Audio Mode On')
        self.assertEqual(self.describe('50:7A:9E'), 'Bridge -> TV: Report Audio Status volume 30, muted')
        self.assertEqual(self.describe('54:90:00'), 'Bridge -> Player 1: Report Power Status on')
        self.assertEqual(self.describe('5F:84:20:00:05'),
                         'Bridge -> all: Report Physical Address 2.0.0.0 (Audio System)')

    def test_tv_routing(self):
        self.assertEqual(self.describe('0F:86:20:00'), 'TV -> all: Set Stream Path 2.0.0.0')
        self.assertEqual(self.describe('0F:80:10:00:20:00'), 'TV -> all: Routing Change 1.0.0.0 to 2.0.0.0')

    def test_keys(self):
        self.assertEqual(self.describe('45:44:6D'), 'Player 1 -> Bridge: Key Pressed Power On')
        self.assertEqual(self.describe('05:44:41'), 'TV -> Bridge: Key Pressed Volume Up')
        self.assertEqual(self.describe('05:44:99'), 'TV -> Bridge: Key Pressed key 0x99')

    def test_vendors(self):
        self.assertEqual(self.describe('0F:87:00:00:F0'), 'TV -> all: Device Vendor ID 00:00:F0 (samsung)')
        self.assertEqual(self.describe('50:A0:00:00:F0:24:00:80'),
                         'Bridge -> TV: Vendor Command With ID 00:00:F0 (samsung) 24:00:80')
        self.assertEqual(self.describe('4F:87:12:34:56'), 'Player 1 -> all: Device Vendor ID 12:34:56')

    def test_feature_abort_names_the_refused_message(self):
        self.assertEqual(self.describe('50:00:C0:00'), 'Bridge -> TV: Feature Abort of Initiate ARC: unrecognized')

    def test_text_operands(self):
        self.assertEqual(self.describe('45:47:46:69:72:65'), "Player 1 -> Bridge: Set OSD Name 'Fire'")

    def test_system_audio_mode_request_without_address_turns_it_off(self):
        self.assertEqual(self.describe('05:70'), 'TV -> Bridge: System Audio Mode Request (off)')

    def test_unknown_opcode_and_short_operands_show_raw_bytes(self):
        self.assertEqual(self.describe('05:E1:01:02'), 'TV -> Bridge: opcode 0xE1 01:02')
        self.assertEqual(self.describe('0F:82:10'), 'TV -> all: Active Source 10')

    def test_ping_and_unregistered_sender(self):
        self.assertEqual(self.describe('05'), 'TV -> Bridge: ping')
        self.assertEqual(self.describe('F5:8F'), 'Unregistered -> Bridge: Give Device Power Status')


class TestInputSwitchNotes(unittest.TestCase):
    """The page has to make the moment the TV moves to the bridge's input easy to spot."""

    def note(self, text, sent=False, bridge=0x2000):
        with mock.patch.object(cec_bridge, 'bridge_phys_addr', bridge):
            return cec_bridge.input_switch_note(frame(text), sent)

    def test_tv_switching_to_the_bridge_input_is_flagged(self):
        self.assertIn('SWITCHING TO THE SONOS BRIDGE', self.note('0F:86:20:00'))
        self.assertIn('SWITCHING TO THE SONOS BRIDGE', self.note('0F:80:10:00:20:00'))
        self.assertIn('SWITCHING TO THE SONOS BRIDGE', self.note('0F:81:20:00'))
        self.assertIn('SWITCHING TO THE SONOS BRIDGE', self.note('4F:82:20:00'))

    def test_other_inputs_are_not_flagged(self):
        self.assertIsNone(self.note('4F:82:10:00'))
        self.assertIsNone(self.note('0F:86:10:00'))
        self.assertIsNone(self.note('0F:80:20:00:10:00'))  # switching away from the bridge

    def test_nothing_is_flagged_before_the_bridge_knows_its_input(self):
        self.assertIsNone(self.note('0F:86:20:00', bridge=cec_bridge.CEC_PHYS_ADDR_INVALID))

    def test_bridge_asking_to_be_shown_is_flagged(self):
        for text in ('5F:82:20:00', '50:04', '50:0D'):
            self.assertIn('BRIDGE ASKED THE TV TO SHOW IT', self.note(text, sent=True))

    def test_truncated_messages_are_not_flagged(self):
        self.assertIsNone(self.note('0F:86:20'))
        self.assertIsNone(self.note('0F'))


class TestTrafficLog(ActivityTestCase):
    def test_received_and_sent_messages(self):
        cec_bridge.log_cec_traffic(frame('45:70:10:00'), sent=False)
        cec_bridge.log_cec_traffic(frame('5F:72:01'), sent=True)
        self.assertEqual(self.logged(), [
            'IN  45:70:10:00       Player 1 -> Bridge: System Audio Mode Request for 1.0.0.0',
            'OUT 5F:72:01          Bridge -> all: Set System Audio Mode On',
        ])

    def test_messages_between_other_devices_have_no_direction(self):
        cec_bridge.log_cec_traffic(frame('40:0D'), sent=False)
        self.assertEqual(self.logged(), ['    40:0D             Player 1 -> TV: Text View On'])

    def test_undelivered_messages_are_marked(self):
        cec_bridge.log_cec_traffic(frame('50:7A:1E'), sent=True, acked=False)
        self.assertTrue(self.logged()[0].endswith('(not delivered)'))

    def test_input_switch_is_flagged_in_both_logs(self):
        cec_bridge.log_cec_traffic(frame('0F:86:20:00'), sent=False)
        self.assertIn('!! TV IS SWITCHING TO THE SONOS BRIDGE INPUT', self.logged()[0])
        cec_bridge.log.warning.assert_called_once()

    def test_pings_are_skipped(self):
        cec_bridge.log_cec_traffic(frame('05'), sent=False)
        self.assertEqual(self.logged(), [])

    def test_learns_the_bridge_input_from_its_own_announcement(self):
        cec_bridge.bridge_phys_addr = cec_bridge.CEC_PHYS_ADDR_INVALID
        cec_bridge.log_cec_traffic(frame('5F:84:30:00:05'), sent=True)
        self.assertEqual(cec_bridge.bridge_phys_addr, 0x3000)

    def test_cec_client_traffic_lines(self):
        cec_bridge.log_cec_client_traffic('TRAFFIC: [  1234]\t>> 45:70:10:00')
        cec_bridge.log_cec_client_traffic('TRAFFIC: [  1240]\t<< 5f:82:20:00')
        cec_bridge.log_cec_client_traffic('NOTICE:  [  1250]\tCEC client registered')
        logged = self.logged()
        self.assertEqual(len(logged), 2)
        self.assertTrue(logged[0].startswith('IN  45:70:10:00'))
        self.assertTrue(logged[1].startswith('OUT 5F:82:20:00'))
        self.assertIn('BRIDGE ASKED THE TV TO SHOW IT', logged[1])

    def test_hdmi_state_lines(self):
        self.assertEqual(cec_bridge.describe_hdmi_state(0x2000, 1 << 5),
                         '--- HDMI connection up: bridge is on TV input 2.0.0.0, '
                         'has the Audio System address ---')
        self.assertEqual(cec_bridge.describe_hdmi_state(0x2000, 0, True),
                         '--- HDMI connection up: bridge is on TV input 2.0.0.0, '
                         'does not have the Audio System address (changed more than once) ---')
        self.assertEqual(cec_bridge.describe_hdmi_state(0xFFFF, 0),
                         '--- HDMI connection down (TV off or cable unplugged) ---')


class TestBridgeLogsTrafficWithoutMonitor(ActivityTestCase):
    """If the monitor can't open, the bridge logs what it sends and receives itself - once."""

    def setUp(self):
        super().setUp()
        self.cec = mock.Mock()
        self.cec.transmit.return_value = True
        for p in (mock.patch.object(cec_bridge, 'cec_dev', self.cec),
                  mock.patch.object(cec_bridge, 'cec_proc', None),
                  mock.patch.object(cec_bridge, 'tv_brand', 'samsung'),
                  mock.patch.object(cec_bridge, 'sonos_queue')):
            p.start()
            self.addCleanup(p.stop)

    def test_logs_received_and_sent_messages(self):
        cec_bridge.handle_cec_frame(frame('05:8F'), SPEAKER_IP)
        self.assertEqual([line.split()[0] for line in self.logged()], ['IN', 'OUT'])

    def test_monitor_logs_them_instead(self):
        cec_bridge.traffic_monitor_active = True
        cec_bridge.handle_cec_frame(frame('05:8F'), SPEAKER_IP)
        self.assertEqual(self.logged(), [])
        self.cec.transmit.assert_called_once_with(frame('50:90:00'))

    def test_a_logging_failure_never_disturbs_the_bridge(self):
        with mock.patch.object(cec_bridge, 'describe_cec_frame', side_effect=RuntimeError('bug')):
            cec_bridge.handle_cec_frame(frame('05:8F'), SPEAKER_IP)
        self.cec.transmit.assert_called_once_with(frame('50:90:00'))  # the TV still gets its answer
        self.assertEqual(cec_bridge.log.warning.call_count, 2)       # and the failure is noted


class FakeMonitorKernel:
    """The kernel side of a monitor handle: queued messages and events, then EAGAIN."""

    def __init__(self, messages=(), events=(), monitor_all=False):
        self.messages = list(messages)
        self.events = list(events)
        self.monitor_all = monitor_all
        self.modes = []

    def ioctl(self, fd, request, arg, *rest):
        if request == cec_bridge.CEC_S_MODE:
            mode = struct.unpack('=I', arg)[0]
            self.modes.append(mode)
            if mode == cec_bridge.CEC_MODE_MONITOR_ALL and not self.monitor_all:
                raise OSError(errno.EINVAL, 'MONITOR_ALL not supported')
            return 0
        queue = self.messages if request == cec_bridge.CEC_RECEIVE else self.events
        if not queue:
            raise OSError(errno.EAGAIN, 'empty')
        arg[:] = queue.pop(0)
        return 0


def monitored_message(text, ts, tx_status=0):
    """A message as the kernel hands it to a monitor: sent ones carry tx_ts, received ones rx_ts."""
    data = frame(text)
    tx_ts, rx_ts = (ts, 0) if tx_status else (0, ts)
    return cec_bridge.CEC_MSG.pack(tx_ts, rx_ts, len(data), 0, 0, 0, data, 0, 0 if tx_status else 1,
                                   tx_status, 0, 0, 0, 0)


def state_event(phys_addr, log_addr_mask, ts, flags=0):
    return cec_bridge.CEC_EVENT.pack(ts, cec_bridge.CEC_EVENT_STATE_CHANGE, flags,
                                     struct.pack('=HHH', phys_addr, log_addr_mask, 0).ljust(64, b'\0'))


class FakePoller:
    def __init__(self, revents):
        self.revents = revents

    def register(self, fd, mask):
        self.mask = mask

    def poll(self, timeout_ms):
        return [(42, self.revents)] if self.revents else []


class TestCECMonitor(unittest.TestCase):
    def open_monitor(self, kernel, revents=select.POLLIN | select.POLLPRI):
        patches = (mock.patch.object(cec_bridge.os, 'open', return_value=42),
                   mock.patch.object(cec_bridge.fcntl, 'ioctl', kernel.ioctl),
                   mock.patch.object(cec_bridge.select, 'poll', return_value=FakePoller(revents)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return cec_bridge.CECMonitor(device='/dev/cec0')

    def test_listens_to_the_whole_bus_when_the_hardware_can(self):
        kernel = FakeMonitorKernel(monitor_all=True)
        monitor = self.open_monitor(kernel)
        self.assertTrue(monitor.whole_bus)
        self.assertEqual(kernel.modes, [0xF0])

    def test_otherwise_listens_to_what_the_bridge_sends_and_receives(self):
        kernel = FakeMonitorKernel()
        monitor = self.open_monitor(kernel)
        self.assertFalse(monitor.whole_bus)
        self.assertEqual(kernel.modes, [0xF0, 0xE0])  # never an initiator: it can't transmit

    def test_setup_failure_closes_device(self):
        def refused(fd, request, arg, *rest):
            raise OSError(errno.EPERM, 'needs CAP_NET_ADMIN')
        with mock.patch.object(cec_bridge.os, 'open', return_value=42), \
                mock.patch.object(cec_bridge.os, 'close') as close, \
                mock.patch.object(cec_bridge.fcntl, 'ioctl', refused):
            with self.assertRaises(OSError):
                cec_bridge.CECMonitor(device='/dev/cec0')
        close.assert_called_once_with(42)

    def test_read_returns_messages_in_both_directions_and_state_changes(self):
        kernel = FakeMonitorKernel(
            messages=[monitored_message('45:70:10:00', ts=200),
                      monitored_message('5F:72:01', ts=300, tx_status=cec_bridge.CEC_TX_STATUS_OK),
                      monitored_message('50:7A:1E', ts=400, tx_status=0x24)],  # NACK | MAX_RETRIES
            events=[state_event(0x2000, 1 << 5, ts=100, flags=cec_bridge.CEC_EVENT_FL_DROPPED_EVENTS)])
        monitor = self.open_monitor(kernel)
        self.assertEqual(monitor.read(timeout_ms=5000), [
            ('state', 0x2000, 1 << 5, True),
            ('message', frame('45:70:10:00'), False, False),
            ('message', frame('5F:72:01'), True, True),
            ('message', frame('50:7A:1E'), True, False),
        ])
        self.assertEqual(monitor.read(timeout_ms=5000), [])  # queues drained

    def test_read_keeps_the_order_things_happened_in(self):
        # messages and events arrive in separate queues; the kernel's timestamps say what came first
        kernel = FakeMonitorKernel(
            messages=[monitored_message('4F:82:10:00', ts=100),
                      monitored_message('5F:84:20:00:05', ts=300, tx_status=cec_bridge.CEC_TX_STATUS_OK)],
            events=[state_event(0xFFFF, 0, ts=150), state_event(0x2000, 0, ts=250)])
        monitor = self.open_monitor(kernel)
        self.assertEqual([item[:2] for item in monitor.read(timeout_ms=5000)], [
            ('message', frame('4F:82:10:00')),
            ('state', 0xFFFF),
            ('state', 0x2000),
            ('message', frame('5F:84:20:00:05')),
        ])

    def test_read_returns_nothing_after_a_quiet_timeout(self):
        monitor = self.open_monitor(FakeMonitorKernel(), revents=0)
        self.assertEqual(monitor.read(timeout_ms=5000), [])

    def test_lost_messages_are_reported(self):
        lost = cec_bridge.CEC_EVENT.pack(0, cec_bridge.CEC_EVENT_LOST_MSGS, 0,
                                         struct.pack('=I', 7).ljust(64, b'\0'))
        monitor = self.open_monitor(FakeMonitorKernel(events=[lost]), revents=select.POLLPRI)
        self.assertEqual(monitor.read(timeout_ms=5000), [('lost', 7)])

    def test_device_going_away_stops_the_monitor(self):
        monitor = self.open_monitor(FakeMonitorKernel(), revents=select.POLLERR | select.POLLPRI)
        with self.assertRaises(OSError):
            monitor.read(timeout_ms=5000)

    def test_struct_size_and_ioctl_number(self):
        # include/uapi/linux/cec.h: struct cec_event is 80 bytes, CEC_DQEVENT = _IOWR('a', 7, ...)
        self.assertEqual(cec_bridge.CEC_EVENT.size, 80)
        self.assertEqual(cec_bridge.CEC_DQEVENT, 0xC0506107)


class TestMonitorThread(ActivityTestCase):
    def test_writes_what_it_sees_to_the_activity_log(self):
        monitor = mock.Mock()
        monitor.read.side_effect = [
            [('state', 0x3000, 1 << 5, False), ('message', frame('0F:86:30:00'), False, False)],
            [('lost', 3)],
            OSError(errno.ENODEV, 'gone'),
        ]
        cec_bridge.traffic_monitor_active = True
        cec_bridge.run_cec_monitor(monitor)
        logged = self.logged()
        self.assertIn('TV input 3.0.0.0', logged[0])
        self.assertEqual(cec_bridge.bridge_phys_addr, 0x3000)
        self.assertIn('!! TV IS SWITCHING TO THE SONOS BRIDGE INPUT', logged[1])
        self.assertEqual(logged[2], '--- 3 messages came too fast to log ---')
        # the bridge takes over logging, and the handle is released
        self.assertFalse(cec_bridge.traffic_monitor_active)
        monitor.close.assert_called_once()


class TestStartCecMonitor(ActivityTestCase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(cec_bridge, 'background_threads', {}),
                  mock.patch.object(cec_bridge, 'Thread')):
            p.start()
            self.addCleanup(p.stop)

    def test_starts_once(self):
        with mock.patch.object(cec_bridge, 'CECMonitor') as monitor_class:
            cec_bridge.start_cec_monitor()
            cec_bridge.start_cec_monitor()  # run_bridge restarting after an error
        monitor_class.assert_called_once_with()
        self.assertTrue(cec_bridge.traffic_monitor_active)
        cec_bridge.Thread.assert_called_once_with(target=cec_bridge.run_cec_monitor,
                                                  args=(monitor_class.return_value,), daemon=True)

    def test_bridge_logs_traffic_itself_when_the_monitor_is_unavailable(self):
        for error in (OSError(errno.EPERM, 'denied'), ValueError('anything else')):
            with mock.patch.object(cec_bridge, 'CECMonitor', side_effect=error):
                cec_bridge.start_cec_monitor()  # must not stop the bridge from starting
            self.assertFalse(cec_bridge.traffic_monitor_active)
        cec_bridge.Thread.assert_not_called()

    def test_monitor_thread_that_cannot_start_is_cleaned_up(self):
        cec_bridge.Thread.return_value.start.side_effect = RuntimeError("can't start new thread")
        with mock.patch.object(cec_bridge, 'CECMonitor') as monitor_class:
            cec_bridge.start_cec_monitor()
        self.assertFalse(cec_bridge.traffic_monitor_active)
        monitor_class.return_value.close.assert_called_once()

    def test_monitor_starts_before_the_bridge_claims_its_address(self):
        # so the activity log shows the bridge announcing itself to the TV
        calls = []
        with mock.patch.object(cec_bridge.os.path, 'exists', return_value=True), \
                mock.patch.object(cec_bridge, 'start_cec_monitor', lambda: calls.append('monitor')), \
                mock.patch.object(cec_bridge, 'KernelCEC', lambda name: calls.append('claim')):
            cec_bridge.open_kernel_cec('Kitchen')
        self.assertEqual(calls, ['monitor', 'claim'])


class TestOpenTrafficLog(unittest.TestCase):
    def test_log_goes_to_its_own_rotating_file(self):
        logger = logging.getLogger('test_cec_traffic')
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cec_bridge, 'traffic_log', logger), \
                mock.patch.object(cec_bridge, 'TRAFFIC_LOG_FILE', os.path.join(tmp, 'cec.log')):
            cec_bridge.open_traffic_log()
            cec_bridge.open_traffic_log()  # run_bridge restarting after an error
            self.assertEqual(len(logger.handlers), 1)
            handler = logger.handlers[0]
            self.assertIsInstance(handler, logging.handlers.RotatingFileHandler)
            self.assertEqual(handler.maxBytes, 512 * 1024)
            logger.removeHandler(handler)
            handler.close()

    def test_unwritable_log_does_not_stop_the_bridge(self):
        logger = logging.getLogger('test_cec_traffic_unwritable')
        with mock.patch.object(cec_bridge, 'traffic_log', logger), \
                mock.patch.object(cec_bridge, 'log'), \
                mock.patch.object(cec_bridge, 'TRAFFIC_LOG_FILE', '/nonexistent-dir/cec.log'):
            cec_bridge.open_traffic_log()
        self.assertEqual(logger.handlers, [])


class TestCecActivityPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'cec.log')
        patch = mock.patch.object(web_server, 'CEC_ACTIVITY_LOG', self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, path, lines):
        with open(path, 'w') as f:
            f.write(''.join(line + '\n' for line in lines))

    def test_newest_first_across_the_rotated_file(self):
        self.write(self.path + '.1', ['one', 'two'])
        self.write(self.path, ['three', 'four'])
        self.assertEqual(web_server.read_cec_activity(), ['four', 'three', 'two', 'one'])
        self.assertEqual(web_server.read_cec_activity(limit=3), ['four', 'three', 'two'])

    def test_no_log_yet(self):
        self.assertEqual(web_server.read_cec_activity(), [])
        page = web_server.render_cec_activity([], '1.5.2')
        self.assertIn('Nothing yet', page)
        self.assertIn('v1.5.2', page)

    def test_page_escapes_and_highlights(self):
        page = web_server.render_cec_activity([
            "09-30 21:04:12.001  IN  0F:86:20:00       TV -> all: Set Stream Path 2.0.0.0   !! TV IS SWITCHING",
            "09-30 21:04:11.230  IN  45:47:3C:62:3E    Player 1 -> Bridge: Set OSD Name '<b>'",
            "09-30 21:04:10.000  --- HDMI connection down (TV off or cable unplugged) ---",
        ], '1.5.2')
        self.assertIn('<span class="flag">', page)
        self.assertIn('<span class="state">', page)
        self.assertIn("&#x27;&lt;b&gt;&#x27;", page)
        self.assertNotIn("'<b>'", page)

    def test_admin_panel_links_to_the_page(self):
        self.assertIn('id="cecBtn"', web_server.ADMIN_PAGE_HTML)
        self.assertIn('window.location.href = "/cec"', web_server.ADMIN_PAGE_HTML)


if __name__ == '__main__':
    unittest.main()
