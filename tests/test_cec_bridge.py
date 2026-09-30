#!/usr/bin/env python3
"""
Tests for cec_bridge.py - run with:  python3 -m unittest discover tests

No Raspberry Pi needed: CEC traffic goes through a fake transport and the
kernel ioctls are patched, so these run on any machine with Python 3.
"""

import errno
import logging
import logging.handlers  # before FileHandler is patched below: its classes subclass it
import os
import queue
import signal
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# cec_bridge logs to /var/log at import time, which only exists on the Pi
with mock.patch('logging.FileHandler', lambda *a, **k: logging.NullHandler()):
    import cec_bridge

logging.disable(logging.CRITICAL)

SPEAKER_IP = '192.168.1.50'

# Opcodes that make a TV switch its input to the sender
INPUT_GRABBING_OPCODES = {
    0x82: '<Active Source>',
    0x04: '<Image View On>',
    0x0D: '<Text View On>',
}


def frame(text):
    """'05:44:41' -> b'\\x05\\x44\\x41'"""
    return bytes(int(b, 16) for b in text.split(':'))


def fake_framebuffer_blank(test):
    """An empty file standing in for /sys/class/graphics/fb0/blank, removed after the test."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = os.path.join(tmp.name, 'blank')
    open(path, 'w').close()
    return path


class FakeCEC:
    """Stands in for the kernel CEC device and records what the bridge sends."""

    def __init__(self, phys_addr=0x2000):
        self.sent = []
        self.phys_addr = phys_addr

    def physical_address(self):
        return self.phys_addr

    def transmit(self, data):
        self.sent.append(bytes(data))
        return True

    def sent_text(self):
        return [':'.join(f'{b:02X}' for b in f) for f in self.sent]


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.cec = FakeCEC()
        patches = [
            mock.patch.object(cec_bridge, 'cec_dev', self.cec),
            mock.patch.object(cec_bridge, 'cec_proc', None),
            mock.patch.object(cec_bridge, 'tv_brand', 'unknown'),
            mock.patch.object(cec_bridge, 'current_volume', 30),
            mock.patch.object(cec_bridge, 'is_muted', False),
            mock.patch.object(cec_bridge, 'last_vol_time', 0),
            mock.patch.object(cec_bridge, 'last_vendor_query', cec_bridge.time.time()),  # just asked
            mock.patch.object(cec_bridge, 'sonos_queue', queue.Queue()),
            mock.patch.object(cec_bridge, 'handle_volume'),
            mock.patch.object(cec_bridge, 'handle_mute'),
            mock.patch.object(cec_bridge, 'screen_owner', None),
            mock.patch.object(cec_bridge, 'pending_snap_back', None),
            mock.patch.object(cec_bridge, 'snap_back_times', []),
            mock.patch.object(cec_bridge, 'picture_wanted', False),
            mock.patch.object(cec_bridge, 'FRAMEBUFFER_BLANK', fake_framebuffer_blank(self)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def picture(self):
        """What the bridge last told the display driver: 'on', 'off', or None if nothing."""
        with open(cec_bridge.FRAMEBUFFER_BLANK) as f:
            return {'0': 'on', '4': 'off', '': None}[f.read()]

    def receive(self, text):
        cec_bridge.handle_cec_frame(frame(text), SPEAKER_IP)

    def run_sonos_queue(self):
        """Do what the Sonos worker thread would do with everything queued so far."""
        while not cec_bridge.sonos_queue.empty():
            cec_bridge.run_next_sonos_action()

    def assert_tv_input_not_claimed(self):
        grabbed = [INPUT_GRABBING_OPCODES[f[1]] for f in self.cec.sent
                   if len(f) > 1 and f[1] in INPUT_GRABBING_OPCODES]
        self.assertEqual(grabbed, [], f"bridge sent {grabbed} - the TV would switch to the bridge")


class TestNeverTakesOverTheTvInput(BridgeTestCase):
    """The bug: pressing Home on the Fire TV remote switched the TV to the bridge's splash screen."""

    def test_power_on_key_sent_to_audio_system_does_not_claim_tv_input(self):
        self.receive('45:44:6D')  # Fire TV -> Audio System: <User Control Pressed> Power On Function
        self.assert_tv_input_not_claimed()
        self.assertEqual(self.cec.sent, [])

    def test_power_toggle_keys_do_not_claim_tv_input(self):
        self.receive('05:44:40')  # TV -> Audio System: Power
        self.receive('05:44:6B')  # TV -> Audio System: Power Toggle Function
        self.assert_tv_input_not_claimed()
        self.assertEqual(self.cec.sent, [])

    def test_set_stream_path_does_not_claim_tv_input(self):
        self.receive('0F:86:20:00')
        self.assert_tv_input_not_claimed()
        self.assertEqual(self.cec.sent, [])

    def test_routing_change_does_not_claim_tv_input(self):
        self.receive('0F:80:10:00:20:00')
        self.assert_tv_input_not_claimed()
        self.assertEqual(self.cec.sent, [])

    def test_request_active_source_gets_no_reply(self):
        self.receive('0F:85')
        self.assertEqual(self.cec.sent, [])

    def test_fire_tv_one_touch_play_does_not_claim_tv_input(self):
        self.receive('4F:82:10:00')  # Fire TV: <Active Source>
        self.receive('45:70:10:00')  # Fire TV -> Audio System: <System Audio Mode Request>
        self.assert_tv_input_not_claimed()


class TestHandsTheScreenBack(BridgeTestCase):
    """The TV switched to the bridge ~9 s after the Fire TV took the screen: the Fire TV
    remote, with HDMI 2 saved as the Fire TV's input, sent it there by infrared.

    The bridge is at 2.0.0.0 (FakeCEC) and the Fire TV at 3.0.0.0, as in the CEC
    activity log that showed it.
    """

    def setUp(self):
        super().setUp()
        self.now = 1000.0
        patch = mock.patch.object(cec_bridge.time, 'time', lambda: self.now)
        patch.start()
        self.addCleanup(patch.stop)

    def at(self, seconds, text=None):
        """Move the clock to `seconds` after the start, receive a message, run the main loop's check."""
        self.now = 1000.0 + seconds
        if text:
            self.receive(text)
        cec_bridge.snap_back_if_due()

    def fire_tv_takes_the_screen_then_tv_jumps_to_the_bridge(self, start=0):
        self.at(start, '4F:82:30:00')                  # Fire TV: <Active Source> 3.0.0.0
        self.at(start + 9.0, '0F:80:30:00:20:00')      # TV: <Routing Change> 3.0.0.0 -> 2.0.0.0
        self.at(start + 9.2, '0F:86:20:00')            # TV: <Set Stream Path> 2.0.0.0

    def test_hands_the_screen_back_to_the_device_the_tv_came_from(self):
        self.fire_tv_takes_the_screen_then_tv_jumps_to_the_bridge()
        self.assertEqual(self.cec.sent, [])            # lets the TV finish switching first
        self.at(10.1)
        self.assertEqual(self.cec.sent_text(), ['5F:86:30:00'])  # <Set Stream Path> 3.0.0.0
        self.at(11.0)
        self.at(20.0, '4F:82:30:00')                   # the Fire TV takes it back
        self.assertEqual(self.cec.sent_text(), ['5F:86:30:00'])  # once
        self.assert_tv_input_not_claimed()
        self.assertFalse(cec_bridge.picture_wanted)    # no splash screen for a jump it didn't ask for

    def test_also_when_the_tv_only_announces_the_stream_path(self):
        self.at(0, '4F:82:30:00')
        self.at(9.0, '0F:86:20:00')
        self.at(10.1)
        self.assertEqual(self.cec.sent_text(), ['5F:86:30:00'])

    def test_bridge_screen_chosen_later_is_left_alone_and_shown(self):
        self.at(0, '4F:82:30:00')
        self.at(cec_bridge.SNAP_BACK_WINDOW + 1, '0F:80:30:00:20:00')
        self.at(cec_bridge.SNAP_BACK_WINDOW + 5)
        self.assertEqual(self.cec.sent, [])
        self.assertEqual(self.picture(), 'on')         # the splash screen, with the admin panel's address

    def test_splash_screen_goes_off_when_the_tv_moves_on(self):
        later = cec_bridge.SNAP_BACK_WINDOW + 1
        self.at(0, '0F:80:00:00:20:00')                # chosen from the TV's own apps
        self.assertEqual(self.picture(), 'on')
        self.at(30, '0F:80:20:00:30:00')               # then the Fire TV input
        self.assertEqual(self.picture(), 'off')
        self.at(30 + later, '0F:86:20:00')             # the bridge again, via the source list
        self.assertEqual(self.picture(), 'on')
        self.at(40 + later, '4F:82:30:00')             # Home on the Fire TV remote
        self.assertEqual(self.picture(), 'off')

    def test_bridge_chosen_right_after_another_device_counts_as_the_tv_jumping(self):
        # Indistinguishable on HDMI-CEC from the Fire TV remote's jump, which came 5-11 s
        # after the Fire TV took the screen: the screen goes back, the picture stays off
        self.at(0, '0F:80:00:00:30:00')                # the Fire TV input
        self.at(10, '0F:80:30:00:20:00')               # the bridge's, 10 s later
        self.at(11.1)
        self.assertEqual(self.cec.sent_text(), ['5F:86:30:00'])
        self.assertNotEqual(self.picture(), 'on')
        self.assertFalse(cec_bridge.picture_wanted)

    def test_splash_screen_goes_off_when_the_tv_switches_off(self):
        self.at(0, '0F:86:20:00')
        self.at(30, '0F:36')                           # <Standby>
        self.assertEqual(self.picture(), 'off')

    def test_splash_screen_shows_when_the_tv_insists_on_the_bridge(self):
        # after SNAP_BACK_LIMIT hand-backs the TV is left on the bridge: better the splash than "no signal"
        for cycle in range(cec_bridge.SNAP_BACK_LIMIT + 1):
            self.fire_tv_takes_the_screen_then_tv_jumps_to_the_bridge(start=cycle * 30)
            self.at(cycle * 30 + 11)
        self.assertEqual(self.picture(), 'on')

    def test_nothing_is_handed_back_once_the_tv_has_moved_on(self):
        self.at(0, '4F:82:30:00')
        self.at(9.0, '0F:80:30:00:20:00')
        self.at(9.5, '0F:80:20:00:30:00')              # back on the Fire TV already
        self.at(12.0)
        self.assertEqual(self.cec.sent, [])

    def test_never_hands_the_screen_to_a_device_the_tv_did_not_come_from(self):
        self.at(0, '4F:82:30:00')
        self.at(5.0, '0F:80:30:00:00:00')              # TV goes to its own apps
        self.at(9.0, '0F:80:00:00:20:00')              # then to the bridge
        self.at(12.0)
        self.assertEqual(self.cec.sent, [])

    def test_garbled_active_source_is_not_taken_for_a_device(self):
        # The log also showed 4F:82:03:00:00:00 - too long for an <Active Source>
        self.at(0, '4F:82:30:00')
        self.at(5.0, '4F:82:03:00:00:00')
        self.at(9.0, '0F:80:30:00:20:00')
        self.at(10.1)
        self.assertEqual(self.cec.sent_text(), ['5F:86:30:00'])

    def test_stops_if_the_tv_keeps_switching_to_the_bridge(self):
        for cycle in range(cec_bridge.SNAP_BACK_LIMIT + 1):
            self.fire_tv_takes_the_screen_then_tv_jumps_to_the_bridge(start=cycle * 30)
            self.at(cycle * 30 + 11)
        self.assertEqual(self.cec.sent_text(), ['5F:86:30:00'] * cec_bridge.SNAP_BACK_LIMIT)

    def test_waits_until_the_bridge_knows_its_input(self):
        self.cec.phys_addr = 0xFFFF
        self.fire_tv_takes_the_screen_then_tv_jumps_to_the_bridge()
        self.at(11.0)
        self.assertEqual(self.cec.sent, [])


class TestKeepsExistingAudioBehaviour(BridgeTestCase):
    def test_volume_keys_do_not_run_on_the_cec_thread(self):
        # A slow Sonos must not delay CEC replies (libCEC used to answer from its own process)
        self.receive('05:44:41')
        cec_bridge.handle_volume.assert_not_called()
        self.assertEqual(cec_bridge.sonos_queue.qsize(), 1)

    def test_volume_up_key_turns_sonos_up(self):
        self.receive('05:44:41')
        self.run_sonos_queue()
        cec_bridge.handle_volume.assert_called_once_with(SPEAKER_IP, 'up')

    def test_volume_down_key_turns_sonos_down(self):
        self.receive('05:44:42')
        self.run_sonos_queue()
        cec_bridge.handle_volume.assert_called_once_with(SPEAKER_IP, 'down')

    def test_mute_key_toggles_sonos_mute(self):
        self.receive('45:44:43')
        self.run_sonos_queue()
        cec_bridge.handle_mute.assert_called_once_with(SPEAKER_IP)

    def test_sonos_errors_do_not_stop_the_worker(self):
        cec_bridge.sonos_queue.put((mock.Mock(side_effect=RuntimeError('speaker offline')), ()))
        cec_bridge.sonos_queue.put((cec_bridge.handle_mute, (SPEAKER_IP,)))
        self.run_sonos_queue()
        cec_bridge.handle_mute.assert_called_once_with(SPEAKER_IP)

    def test_volume_keys_are_not_feature_aborted(self):
        self.receive('05:44:41')
        self.receive('05:45')
        self.assertEqual(self.cec.sent, [])

    def test_system_audio_mode_request_turns_mode_on_and_reports_volume(self):
        self.receive('05:70:10:00')
        self.assertEqual(self.cec.sent_text(), ['5F:72:01', '50:7A:1E'])

    def test_give_audio_status_reports_volume(self):
        self.receive('05:71')
        self.assertEqual(self.cec.sent_text(), ['50:7A:1E'])

    def test_give_system_audio_mode_status_reports_on(self):
        self.receive('05:7D')
        self.assertEqual(self.cec.sent_text(), ['50:7E:01'])

    def test_queries_with_extra_operands_are_still_answered(self):
        # CEC says followers ignore extra operands; libCEC answered these
        self.receive('05:7D:00')
        self.receive('05:71:00')
        self.assertEqual(self.cec.sent_text(), ['50:7E:01', '50:7A:1E'])

    def test_lg_arc_request_with_extra_operand_is_still_accepted(self):
        self.receive('0F:87:00:E0:91')
        self.cec.sent.clear()
        self.receive('05:C3:00')
        self.assertEqual(self.cec.sent_text(), ['50:C1'])

    def test_audio_status_goes_to_the_device_that_asked(self):
        self.receive('45:71')  # Fire TV asks; libCEC used to answer it directly
        self.assertEqual(self.cec.sent_text(), ['54:7A:1E'])

    def test_system_audio_mode_status_goes_to_the_device_that_asked(self):
        self.receive('45:7D')
        self.assertEqual(self.cec.sent_text(), ['54:7E:01'])

    def test_short_audio_descriptor_request_is_declined(self):
        self.receive('05:A4:02')
        self.assertEqual(self.cec.sent_text(), ['50:00:A4:00'])

    def test_samsung_tv_arc_request_is_declined(self):
        self.receive('0F:87:00:00:F0')  # Samsung vendor ID
        self.cec.sent.clear()
        self.receive('05:C3')
        self.assertEqual(self.cec.sent_text(), ['50:00:C3:00'])

    def test_lg_tv_arc_request_is_accepted(self):
        self.receive('0F:87:00:E0:91')  # LG vendor ID
        self.cec.sent.clear()
        self.receive('05:C3')
        self.assertEqual(self.cec.sent_text(), ['50:C1'])


class TestTvBrandDetection(BridgeTestCase):
    """libCEC used to Feature Abort every ARC request, which hid what happens when
    the TV brand isn't known yet - accepting ARC makes a Samsung drop the bridge."""

    def test_unlisted_tv_brand_is_other_not_unknown(self):
        self.receive('0F:87:12:34:56')
        self.assertEqual(cec_bridge.tv_brand, 'other')

    def test_other_brand_tv_arc_request_is_accepted(self):
        self.receive('0F:87:12:34:56')
        self.cec.sent.clear()
        self.receive('05:C3')
        self.assertEqual(self.cec.sent_text(), ['50:C1'])

    def test_arc_is_declined_until_the_tv_identifies_itself(self):
        self.receive('05:C3')
        self.assertIn('50:00:C3:00', self.cec.sent_text())
        self.assertNotIn('50:C1', self.cec.sent_text())

    def test_asks_tv_for_its_vendor_id_when_it_speaks_before_identifying(self):
        cec_bridge.last_vendor_query = 0
        self.receive('05:71')
        self.assertEqual(self.cec.sent_text(), ['50:8C', '50:7A:1E'])

    def test_asks_at_most_once_per_interval(self):
        cec_bridge.last_vendor_query = 0
        self.receive('05:71')
        self.receive('05:71')
        self.assertEqual(self.cec.sent_text().count('50:8C'), 1)

    def test_does_not_ask_once_the_tv_is_identified(self):
        self.receive('0F:87:00:00:F0')
        cec_bridge.last_vendor_query = 0
        self.cec.sent.clear()
        self.receive('05:71')
        self.assertEqual(self.cec.sent_text(), ['50:7A:1E'])

    def test_does_not_ask_when_other_devices_speak(self):
        cec_bridge.last_vendor_query = 0
        self.receive('45:71')
        self.assertEqual(self.cec.sent_text(), ['54:7A:1E'])


class TestAnswersWhatLibcecUsedToAnswer(BridgeTestCase):
    """cec-client answered these for us; the kernel device leaves them to the bridge."""

    def test_reports_power_on_to_tv(self):
        self.receive('05:8F')
        self.assertEqual(self.cec.sent_text(), ['50:90:00'])

    def test_reports_power_on_to_fire_tv(self):
        self.receive('45:8F')
        self.assertEqual(self.cec.sent_text(), ['54:90:00'])

    def test_answers_menu_request(self):
        self.receive('05:8D:02')
        self.assertEqual(self.cec.sent_text(), ['50:8E:00'])

    def test_answers_menu_deactivate_request_with_deactivated(self):
        self.receive('05:8D:01')
        self.assertEqual(self.cec.sent_text(), ['50:8E:01'])

    def test_declines_samsung_power_up_vendor_command(self):
        # Answering it as Samsung's soundbars do (24:00:80) invites a Samsung TV to
        # switch to the bridge's input after it wakes, as it does for its soundbars
        self.receive('05:A0:00:00:F0:23')
        self.assertEqual(self.cec.sent_text(), ['50:00:A0:03'])  # Feature Abort: invalid operand

    def test_announces_vendor_id_when_tv_announces_its_own(self):
        self.receive('0F:87:00:00:F0')
        self.assertEqual(self.cec.sent_text(), ['5F:87:00:15:82'])
        self.assertEqual(cec_bridge.tv_brand, 'samsung')

    def test_reports_own_address_when_tv_reports_its_own(self):
        self.receive('0F:84:00:00:00')  # TV powering on announces itself
        self.assertEqual(self.cec.sent_text(), ['5F:84:20:00:05'])  # 2.0.0.0, Audio System

    def test_does_not_report_address_for_other_devices(self):
        self.receive('4F:84:10:00:04')
        self.assertEqual(self.cec.sent, [])

    def test_does_not_report_address_while_hdmi_is_down(self):
        self.cec.phys_addr = 0xFFFF
        self.receive('0F:84:00:00:00')
        self.assertEqual(self.cec.sent, [])

    def test_does_not_announce_vendor_id_for_other_devices(self):
        self.receive('4F:87:00:00:00')
        self.assertEqual(self.cec.sent, [])

    def test_feature_aborts_unsupported_message_to_sender(self):
        self.receive('45:1A:01')  # Fire TV -> Audio System: <Give Deck Status>
        self.assertEqual(self.cec.sent_text(), ['54:00:1A:00'])

    def test_rejects_lg_vendor_commands_as_invalid_operand(self):
        self.receive('05:89:01')  # LG SimpLink init - libCEC's reply to this led to auto-activation too
        self.assertEqual(self.cec.sent_text(), ['50:00:89:03'])

    def test_rejects_other_samsung_vendor_commands_as_invalid_operand(self):
        # Reason 3, like libCEC: "unrecognized opcode" would reject the whole A0 channel
        self.receive('05:A0:00:00:F0:99')
        self.assertEqual(self.cec.sent_text(), ['50:00:A0:03'])

    def test_ignores_unsupported_broadcasts(self):
        self.receive('0F:A0:00:00:00:01')
        self.assertEqual(self.cec.sent, [])

    def test_never_answers_a_feature_abort(self):
        self.receive('05:00:8F:00')
        self.assertEqual(self.cec.sent, [])

    def test_does_not_feature_abort_status_reports(self):
        for report in ('05:C1', '05:C2', '05:90:00', '05:7A:1E', '05:7E:01',
                       '05:47:54:56', '05:9E:05', '05:8E:00', '05:8A:41', '05:8B:41',
                       '05:36', '05:04', '05:0D', '05:72:01'):
            self.receive(report)
        self.assertEqual(self.cec.sent, [])

    def test_ignores_messages_from_unregistered_devices(self):
        self.receive('F5:8F')
        self.assertEqual(self.cec.sent, [])

    def test_startup_asks_tv_for_vendor_id_and_reports_name(self):
        cec_bridge.announce_to_tv('Kitchen')
        self.assertEqual(self.cec.sent_text(), ['50:8C', '50:47:4B:69:74:63:68:65:6E'])


class TestCommandTranslation(unittest.TestCase):
    def test_parse_tx_command(self):
        self.assertEqual(cec_bridge.parse_tx_command('tx 5F:72:01'), b'\x5f\x72\x01')
        self.assertEqual(cec_bridge.parse_tx_command('tx 50:00:c3:00'), b'\x50\x00\xc3\x00')

    def test_format_cec_frame_matches_cec_client_traffic(self):
        self.assertEqual(cec_bridge.format_cec_frame(b'\x0f\x87\x00\x00\xf0'), '>> 0f:87:00:00:f0')

    def test_send_cec_command_uses_kernel_device_when_open(self):
        fake = FakeCEC()
        with mock.patch.object(cec_bridge, 'cec_dev', fake):
            cec_bridge.send_cec_command('tx 50:7E:01')
        self.assertEqual(fake.sent, [b'\x50\x7e\x01'])

    def test_send_cec_command_falls_back_to_cec_client(self):
        proc = mock.Mock()
        with mock.patch.object(cec_bridge, 'cec_dev', None), \
                mock.patch.object(cec_bridge, 'cec_proc', proc):
            cec_bridge.send_cec_command('tx 50:7E:01')
        proc.stdin.write.assert_called_once_with('tx 50:7E:01\n')


class TestLinuxCecApi(unittest.TestCase):
    """Layouts and ioctl numbers from include/uapi/linux/cec.h."""

    def test_struct_sizes(self):
        self.assertEqual(cec_bridge.CEC_MSG.size, 56)
        self.assertEqual(cec_bridge.CEC_LOG_ADDRS.size, 92)

    def test_ioctl_numbers(self):
        self.assertEqual(cec_bridge.CEC_ADAP_G_PHYS_ADDR, 0x80026101)
        self.assertEqual(cec_bridge.CEC_ADAP_S_LOG_ADDRS, 0xC05C6104)
        self.assertEqual(cec_bridge.CEC_TRANSMIT, 0xC0386105)
        self.assertEqual(cec_bridge.CEC_RECEIVE, 0xC0386106)
        self.assertEqual(cec_bridge.CEC_S_MODE, 0x40046109)

    def test_log_addrs_claims_audio_system_like_libcec_did(self):
        fields = cec_bridge.CEC_LOG_ADDRS.unpack(cec_bridge.pack_log_addrs('Kitchen'))
        (_, _, cec_version, num_log_addrs, vendor_id, flags, osd_name,
         primary_device_type, log_addr_type, all_device_types, features) = fields
        self.assertEqual(cec_version, 5)             # CEC 1.4
        self.assertEqual(num_log_addrs, 1)
        self.assertEqual(vendor_id, 0x001582)        # same vendor ID libCEC announced
        self.assertEqual(flags, 0)
        self.assertEqual(osd_name.rstrip(b'\x00'), b'Kitchen')
        self.assertEqual(primary_device_type[0], 5)  # CEC_OP_PRIM_DEVTYPE_AUDIOSYSTEM
        self.assertEqual(log_addr_type[0], 4)        # CEC_LOG_ADDR_TYPE_AUDIOSYSTEM
        self.assertEqual(all_device_types[0], 0x08)  # CEC_OP_ALL_DEVTYPE_AUDIOSYSTEM
        self.assertEqual(features, bytes(48))

    def test_log_addrs_truncates_osd_name_to_14_chars(self):
        fields = cec_bridge.CEC_LOG_ADDRS.unpack(cec_bridge.pack_log_addrs('A' * 20))
        self.assertEqual(fields[6], b'A' * 14 + b'\x00')

    def test_clearing_log_addrs_is_all_zero(self):
        self.assertEqual(cec_bridge.pack_log_addrs(None), bytearray(92))


class FakeIoctl:
    """Records ioctl calls and lets a test fill in what the kernel would return."""

    def __init__(self, on_receive=None, on_transmit=None, claims=True, phys_addr=0x2000):
        self.calls = []
        self.on_receive = on_receive
        self.on_transmit = on_transmit
        self.claims = claims          # whether claiming address 5 succeeds
        self.phys_addr = phys_addr    # 0xFFFF = HDMI unplugged / TV off

    def __call__(self, fd, request, arg, *rest):
        self.calls.append((request, bytes(arg)))
        if request == cec_bridge.CEC_ADAP_S_LOG_ADDRS and self.claims:
            fields = list(cec_bridge.CEC_LOG_ADDRS.unpack(arg))
            if fields[3]:              # num_log_addrs
                fields[1] = 1 << 5     # log_addr_mask: claimed the Audio System address
                cec_bridge.CEC_LOG_ADDRS.pack_into(arg, 0, *fields)
        if request == cec_bridge.CEC_ADAP_G_PHYS_ADDR:
            arg[:] = self.phys_addr.to_bytes(2, sys.byteorder)
        if request == cec_bridge.CEC_RECEIVE and self.on_receive:
            self.on_receive(arg)
        if request == cec_bridge.CEC_TRANSMIT and self.on_transmit:
            self.on_transmit(arg)
        return 0


class TestKernelCEC(unittest.TestCase):
    def open_device(self, ioctl):
        with mock.patch.object(cec_bridge.os, 'open', return_value=42), \
                mock.patch.object(cec_bridge.fcntl, 'ioctl', ioctl):
            return cec_bridge.KernelCEC('Kitchen', device='/dev/cec0')

    def test_setup_claims_audio_system_as_exclusive_follower(self):
        ioctl = FakeIoctl()
        self.open_device(ioctl)
        requests = [request for request, _ in ioctl.calls]
        self.assertEqual(requests, [cec_bridge.CEC_S_MODE,
                                    cec_bridge.CEC_ADAP_S_LOG_ADDRS,
                                    cec_bridge.CEC_ADAP_S_LOG_ADDRS])
        mode = int.from_bytes(ioctl.calls[0][1], sys.byteorder)
        self.assertEqual(mode, 0x21)  # CEC_MODE_INITIATOR | CEC_MODE_EXCL_FOLLOWER
        self.assertEqual(ioctl.calls[1][1], bytes(92))  # clear whatever cec-client configured
        self.assertEqual(ioctl.calls[2][1], bytes(cec_bridge.pack_log_addrs('Kitchen')))

    def test_setup_failure_closes_device(self):
        def failing_ioctl(fd, request, arg, *rest):
            raise OSError(errno.EBUSY, 'busy')
        with mock.patch.object(cec_bridge.os, 'open', return_value=42), \
                mock.patch.object(cec_bridge.os, 'close') as close, \
                mock.patch.object(cec_bridge.fcntl, 'ioctl', failing_ioctl):
            with self.assertRaises(OSError):
                cec_bridge.KernelCEC('Kitchen', device='/dev/cec0')
        close.assert_called_once_with(42)

    def test_setup_fails_loudly_when_audio_system_address_is_taken(self):
        # e.g. a real soundbar already holds address 5: the kernel returns success but claims nothing
        with mock.patch.object(cec_bridge.os, 'open', return_value=42), \
                mock.patch.object(cec_bridge.os, 'close') as close, \
                mock.patch.object(cec_bridge.fcntl, 'ioctl', FakeIoctl(claims=False)):
            with self.assertRaises(OSError):
                cec_bridge.KernelCEC('Kitchen', device='/dev/cec0')
        close.assert_called_once_with(42)

    def test_setup_waits_for_hdmi_when_tv_is_off(self):
        # No physical address yet: the kernel claims address 5 once HDMI comes up
        self.open_device(FakeIoctl(claims=False, phys_addr=0xFFFF))

    def test_physical_address_reads_kernel_value(self):
        dev = self.open_device(FakeIoctl())
        with mock.patch.object(cec_bridge.fcntl, 'ioctl', FakeIoctl(phys_addr=0x2000)):
            self.assertEqual(dev.physical_address(), 0x2000)

    def test_physical_address_is_formatted_like_hdmi_port(self):
        self.assertEqual(cec_bridge.format_physical_address(0x2000), '2.0.0.0')
        self.assertEqual(cec_bridge.format_physical_address(0xFFFF), 'f.f.f.f')

    def test_receive_decodes_frame(self):
        def deliver(buf):
            cec_bridge.CEC_MSG.pack_into(buf, 0, 0, 0, 3, 0, 0, 0, b'\x05\x44\x41',
                                         0, 1, 0, 0, 0, 0, 0)
        dev = self.open_device(FakeIoctl())
        with mock.patch.object(cec_bridge.fcntl, 'ioctl', FakeIoctl(on_receive=deliver)):
            self.assertEqual(dev.receive(timeout_ms=1000), b'\x05\x44\x41')

    def test_receive_passes_timeout_to_kernel(self):
        dev = self.open_device(FakeIoctl())
        ioctl = FakeIoctl()
        with mock.patch.object(cec_bridge.fcntl, 'ioctl', ioctl):
            dev.receive(timeout_ms=1000)
        self.assertEqual(cec_bridge.CEC_MSG.unpack(ioctl.calls[0][1])[3], 1000)

    def test_receive_returns_none_on_timeout(self):
        def timeout(fd, request, arg, *rest):
            raise OSError(errno.ETIMEDOUT, 'timed out')
        dev = self.open_device(FakeIoctl())
        with mock.patch.object(cec_bridge.fcntl, 'ioctl', timeout):
            self.assertIsNone(dev.receive(timeout_ms=1000))

    def test_transmit_sends_frame_and_reports_success(self):
        def acked(buf):
            fields = list(cec_bridge.CEC_MSG.unpack(buf))
            fields[9] = 0x01  # CEC_TX_STATUS_OK
            cec_bridge.CEC_MSG.pack_into(buf, 0, *fields)
        dev = self.open_device(FakeIoctl())
        ioctl = FakeIoctl(on_transmit=acked)
        with mock.patch.object(cec_bridge.fcntl, 'ioctl', ioctl):
            self.assertTrue(dev.transmit(b'\x50\x7a\x1e'))
        fields = cec_bridge.CEC_MSG.unpack(ioctl.calls[0][1])
        self.assertEqual(fields[2], 3)
        self.assertEqual(fields[6][:3], b'\x50\x7a\x1e')

    def test_transmit_failure_is_reported_not_raised(self):
        def unconfigured(fd, request, arg, *rest):
            raise OSError(getattr(errno, 'ENONET', 64), 'HDMI unplugged')  # ENONET is Linux-only
        dev = self.open_device(FakeIoctl())
        with mock.patch.object(cec_bridge.fcntl, 'ioctl', unconfigured):
            self.assertFalse(dev.transmit(b'\x5f\x72\x01'))


class FakeKernelDevice(FakeCEC):
    """KernelCEC stand-in that delivers some frames, then stops the loop like Ctrl+C."""

    def __init__(self, frames):
        super().__init__()
        self.frames = list(frames)
        self.closed = False

    def physical_address(self):
        return 0x2000

    def receive(self, timeout_ms):
        if self.frames:
            return self.frames.pop(0)
        raise KeyboardInterrupt

    def close(self):
        self.closed = True


class TestRunBridge(unittest.TestCase):
    CONFIG = {'speaker_ip': SPEAKER_IP, 'speaker_name': 'Kitchen'}

    def setUp(self):
        patches = [
            mock.patch.object(cec_bridge, 'cec_dev', None),
            mock.patch.object(cec_bridge, 'cec_proc', None),
            mock.patch.object(cec_bridge, 'last_vol_time', 0),
            mock.patch.object(cec_bridge, 'sync_volume_from_sonos'),
            mock.patch.object(cec_bridge, 'Thread'),  # no background threads
            mock.patch.object(cec_bridge, 'background_threads', {}),
            mock.patch.object(cec_bridge, 'sonos_queue', queue.Queue()),
            mock.patch.object(cec_bridge, 'handle_volume'),
            mock.patch.object(cec_bridge, 'hold_hdmi_connection', return_value=True),  # not this machine's HDMI
            mock.patch.object(cec_bridge, 'FRAMEBUFFER_BLANK', fake_framebuffer_blank(self)),  # nor its display
            mock.patch.object(cec_bridge, 'picture_wanted', False),
            mock.patch.object(cec_bridge, 'screen_owner', None),
            mock.patch.object(cec_bridge, 'pending_snap_back', None),
            mock.patch.object(cec_bridge, 'snap_back_times', []),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def picture(self):
        with open(cec_bridge.FRAMEBUFFER_BLANK) as f:
            return {'0': 'on', '4': 'off', '': None}[f.read()]

    def test_splash_screen_is_off_until_the_bridge_input_is_chosen(self):
        with mock.patch.object(cec_bridge, 'open_kernel_cec', return_value=FakeKernelDevice([frame('05:71')])):
            cec_bridge.run_bridge(self.CONFIG)
        self.assertEqual(self.picture(), 'off')

    def test_splash_screen_state_is_reapplied(self):
        # fbi setting its mode, or a reconnect, can turn the display back on behind the bridge's back
        writes = []
        with mock.patch.object(cec_bridge, 'open_kernel_cec',
                               return_value=FakeKernelDevice([frame('05:71'), frame('05:71')])), \
                mock.patch.object(cec_bridge, 'PICTURE_REFRESH', 0), \
                mock.patch.object(cec_bridge, 'apply_picture', side_effect=lambda: writes.append(1)):
            cec_bridge.run_bridge(self.CONFIG)
        self.assertGreaterEqual(len(writes), 3)  # at start, on holding the connection, and each loop

    def hdmi_holds(self):
        return [c.args[0] for c in cec_bridge.hold_hdmi_connection.call_args_list]

    def test_holds_the_hdmi_connection_and_lets_go_on_exit(self):
        # so the TV's brief cuts while it wakes don't drop the bridge off HDMI-CEC
        with mock.patch.object(cec_bridge, 'open_kernel_cec',
                               return_value=FakeKernelDevice([frame('05:8F'), frame('05:71')])):
            cec_bridge.run_bridge(self.CONFIG)
        self.assertEqual(self.hdmi_holds(), [True, False])

    def test_holds_the_hdmi_connection_only_once_it_has_a_tv_input(self):
        dev = FakeKernelDevice([frame('05:8F'), frame('05:71')])
        addresses = iter([0xFFFF, 0xFFFF, 0x2000])  # TV off at first
        dev.physical_address = lambda: next(addresses, 0x2000)
        with mock.patch.object(cec_bridge, 'open_kernel_cec', return_value=dev):
            cec_bridge.run_bridge(self.CONFIG)
        self.assertEqual(self.hdmi_holds(), [True, False])
        self.assertEqual(dev.sent_text()[-2], '50:90:00')          # answered while waiting for HDMI
        self.assertTrue(dev.sent_text()[-1].startswith('50:7A:'))  # and after holding it

    def test_lets_go_and_holds_again_if_the_tv_input_is_lost_anyway(self):
        # e.g. the TV's EDID couldn't be read while holding: the kernel must be able to find it again
        dev = FakeKernelDevice([frame('05:8F'), frame('05:8F'), frame('05:8F')])
        addresses = iter([0x2000, 0x2000, 0xFFFF, 0x2000])
        dev.physical_address = lambda: next(addresses, 0x2000)
        with mock.patch.object(cec_bridge, 'open_kernel_cec', return_value=dev):
            cec_bridge.run_bridge(self.CONFIG)
        self.assertEqual(self.hdmi_holds(), [True, False, True, False])

    def test_main_loop_hands_the_screen_back(self):
        dev = FakeKernelDevice([frame('4F:82:30:00'), frame('0F:80:30:00:20:00'), frame('05:71')])
        with mock.patch.object(cec_bridge, 'open_kernel_cec', return_value=dev), \
                mock.patch.object(cec_bridge, 'SNAP_BACK_DELAY', 0):
            cec_bridge.run_bridge(self.CONFIG)
        self.assertIn('5F:86:30:00', dev.sent_text())
        self.assertEqual(self.picture(), 'off')

    def test_a_failed_hold_is_neither_retried_nor_released(self):
        cec_bridge.hold_hdmi_connection.return_value = False
        with mock.patch.object(cec_bridge, 'open_kernel_cec',
                               return_value=FakeKernelDevice([frame('05:8F'), frame('05:71')])):
            cec_bridge.run_bridge(self.CONFIG)
        self.assertEqual(self.hdmi_holds(), [True])

    def test_cec_client_fallback_leaves_the_hdmi_connection_alone(self):
        proc = mock.Mock()
        proc.stdout = iter([])
        with mock.patch.object(cec_bridge, 'open_kernel_cec', return_value=None), \
                mock.patch.object(cec_bridge.subprocess, 'Popen', return_value=proc):
            cec_bridge.run_bridge(self.CONFIG)
        self.assertEqual(self.hdmi_holds(), [])

    def test_uses_kernel_device_and_closes_it_on_exit(self):
        dev = FakeKernelDevice([frame('05:44:41'), frame('45:44:6D'), frame('05:8F')])
        with mock.patch.object(cec_bridge, 'open_kernel_cec', return_value=dev), \
                mock.patch.object(cec_bridge.subprocess, 'Popen') as popen:
            cec_bridge.run_bridge(self.CONFIG)
            self.assertIsNone(cec_bridge.cec_dev)
        popen.assert_not_called()
        self.run_sonos_queue()
        cec_bridge.handle_volume.assert_called_once_with(SPEAKER_IP, 'up')
        self.assertEqual(dev.sent_text(), ['50:8C', '50:47:4B:69:74:63:68:65:6E', '50:90:00'])
        self.assertTrue(dev.closed)

    def run_sonos_queue(self):
        while not cec_bridge.sonos_queue.empty():
            cec_bridge.run_next_sonos_action()

    def test_starts_sonos_worker_and_wifi_watchdog_threads(self):
        with mock.patch.object(cec_bridge, 'open_kernel_cec', return_value=FakeKernelDevice([])):
            cec_bridge.run_bridge(self.CONFIG)
        targets = [c.kwargs.get('target') for c in cec_bridge.Thread.call_args_list]
        self.assertIn(cec_bridge.sonos_worker, targets)
        self.assertIn(cec_bridge.wifi_watchdog, targets)

    def test_device_is_closed_if_startup_fails_after_opening_it(self):
        # A leaked fd stays exclusive follower, so every later open would fail with EBUSY
        dev = FakeKernelDevice([])
        cec_bridge.Thread.side_effect = RuntimeError("can't start new thread")
        with mock.patch.object(cec_bridge, 'open_kernel_cec', return_value=dev):
            with self.assertRaises(RuntimeError):
                cec_bridge.run_bridge(self.CONFIG)
        self.assertTrue(dev.closed)

    def test_falls_back_to_cec_client_without_kernel_device(self):
        proc = mock.Mock()
        proc.stdout = iter(['TRAFFIC: [   1234]\t>> 05:44:42\n'])
        with mock.patch.object(cec_bridge, 'open_kernel_cec', return_value=None), \
                mock.patch.object(cec_bridge.subprocess, 'Popen', return_value=proc) as popen:
            cec_bridge.run_bridge(self.CONFIG)
        self.assertEqual(popen.call_args[0][0][0], 'cec-client')
        self.run_sonos_queue()
        cec_bridge.handle_volume.assert_called_once_with(SPEAKER_IP, 'down')
        proc.terminate.assert_called_once()


class TestSplashScreenSwitch(unittest.TestCase):
    """The display driver's blank switch: 4 turns the HDMI picture off, 0 back on."""

    def setUp(self):
        for p in (mock.patch.object(cec_bridge, 'FRAMEBUFFER_BLANK', fake_framebuffer_blank(self)),
                  mock.patch.object(cec_bridge, 'picture_wanted', False),
                  mock.patch.object(cec_bridge, 'log'),
                  mock.patch.object(cec_bridge, 'traffic_log')):
            p.start()
            self.addCleanup(p.stop)

    def written(self):
        with open(cec_bridge.FRAMEBUFFER_BLANK) as f:
            return f.read()

    def test_on_and_off(self):
        cec_bridge.set_picture(True)
        self.assertEqual(self.written(), '0')
        cec_bridge.set_picture(False)
        self.assertEqual(self.written(), '4')

    def test_changes_go_to_the_activity_log(self):
        cec_bridge.set_picture(True)
        cec_bridge.set_picture(True)
        cec_bridge.set_picture(False)
        lines = [c.args[0] for c in cec_bridge.traffic_log.info.call_args_list]
        self.assertEqual(lines, ["--- Splash screen on: the TV is showing the bridge's input ---",
                                 "--- Splash screen off ---"])

    def test_a_display_without_the_switch_is_reported_not_raised(self):
        with mock.patch.object(cec_bridge, 'FRAMEBUFFER_BLANK', '/nonexistent/fb0/blank'):
            cec_bridge.set_picture(True)
        cec_bridge.log.warning.assert_called_once()


class TestHoldHdmiConnection(unittest.TestCase):
    """The kernel's DRM sysfs switch for the HDMI port: 'on' holds it, 'detect' lets go."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(cec_bridge, 'HDMI_CONNECTOR_STATUS',
                                    os.path.join(self.tmp.name, 'card*-HDMI-A-1', 'status')),
                  mock.patch.object(cec_bridge, 'log')):
            p.start()
            self.addCleanup(p.stop)

    def connector(self, card='card1'):
        path = os.path.join(self.tmp.name, f'{card}-HDMI-A-1', 'status')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f:
            f.write('connected\n')
        return path

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_hold_and_release(self):
        status = self.connector()
        self.assertTrue(cec_bridge.hold_hdmi_connection(True))
        self.assertEqual(self.read(status), 'on')
        self.assertTrue(cec_bridge.hold_hdmi_connection(False))
        self.assertEqual(self.read(status), 'detect')

    def test_no_hdmi_connector(self):
        self.assertFalse(cec_bridge.hold_hdmi_connection(True))
        cec_bridge.log.warning.assert_called_once()

    def test_kernel_refusing_is_reported_not_raised(self):
        self.connector()
        with mock.patch('builtins.open', side_effect=PermissionError(errno.EACCES, 'denied')):
            self.assertFalse(cec_bridge.hold_hdmi_connection(True))
        cec_bridge.log.warning.assert_called_once()


class TestWifiWatchdog(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(cec_bridge, 'wifi_fail_count', 0),
                  mock.patch.object(cec_bridge.os, 'system')):
            p.start()
            self.addCleanup(p.stop)

    def test_reboots_after_repeated_failures(self):
        with mock.patch.object(cec_bridge, 'is_wifi_connected', return_value=False):
            for _ in range(cec_bridge.WIFI_FAIL_THRESHOLD):
                cec_bridge.check_wifi()
        cec_bridge.os.system.assert_called_once_with('reboot')

    def test_a_good_check_resets_the_count(self):
        with mock.patch.object(cec_bridge, 'is_wifi_connected', side_effect=[False, False, True, False, False]):
            for _ in range(5):
                cec_bridge.check_wifi()
        cec_bridge.os.system.assert_not_called()


class TestSignals(unittest.TestCase):
    def test_sigterm_exits_through_cleanup(self):
        # systemctl restart (used by OTA updates) sends SIGTERM; exiting via SystemExit runs finally
        with mock.patch.object(cec_bridge.signal, 'signal') as install:
            cec_bridge.install_signal_handlers()
        signum, handler = install.call_args[0]
        self.assertEqual(signum, signal.SIGTERM)
        with self.assertRaises(SystemExit):
            handler(signum, None)


class TestOpenKernelCec(unittest.TestCase):
    def test_returns_none_without_kernel_device(self):
        with mock.patch.object(cec_bridge.os.path, 'exists', return_value=False):
            self.assertIsNone(cec_bridge.open_kernel_cec('Kitchen'))

    def test_setup_errors_are_raised_so_main_retries_instead_of_using_cec_client(self):
        # cec-client on a present /dev/cec0 would bring back the input switching
        with mock.patch.object(cec_bridge.os.path, 'exists', return_value=True), \
                mock.patch.object(cec_bridge, 'KernelCEC', side_effect=OSError(errno.EBUSY, 'busy')):
            with self.assertRaises(OSError):
                cec_bridge.open_kernel_cec('Kitchen')


if __name__ == '__main__':
    unittest.main()
