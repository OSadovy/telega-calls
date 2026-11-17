"""Unit tests for the helper's own logic.  Run with the interpreter that has
the requirements installed:  python -m unittest discover helper/tests"""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import telega_calls_helper as helper  # noqa: E402

class RtcServers(unittest.TestCase):
    def test_reflector(self):
        [s] = helper.rtc_servers([{
            "id": "1234567890123", "ip_address": "192.0.2.10",
            "ipv6_address": "2001:db8::10", "port": 597,
            "type": {"@type": "callServerTypeTelegramReflector",
                     "peer_tag": base64.b64encode(b"t" * 16).decode(),
                     "is_tcp": False}}])
        self.assertEqual((s.id, s.ipv4, s.ipv6, s.port), (1234567890123, "192.0.2.10", "2001:db8::10", 597))
        self.assertEqual((s.turn, s.stun, s.tcp, s.peer_tag), (True, False, False, b"t" * 16))
        self.assertIsNone(s.username)

    def test_webrtc(self):
        [s] = helper.rtc_servers([{
            "id": 1234567890124, "ip_address": "192.0.2.11", "port": 1400,
            "type": {"@type": "callServerTypeWebrtc", "username": "u",
                     "password": "p", "supports_turn": True, "supports_stun": True}}])
        self.assertEqual((s.username, s.password, s.turn, s.stun), ("u", "p", True, True))
        self.assertEqual((s.ipv6, s.tcp, s.peer_tag), ("", False, None))

    def test_missing_booleans_read_as_false(self):
        # Emacs serialises TDLib's false as false, but an omitted field
        # must not raise either.
        [s] = helper.rtc_servers([{"id": 1, "port": 1, "type": {
            "@type": "callServerTypeWebrtc", "username": "", "password": ""}}])
        self.assertEqual((s.turn, s.stun, s.username), (False, False, None))


class DefaultDevice(unittest.TestCase):
    Device = type("Device", (), {"__init__": lambda self, name: setattr(self, "name", name)})

    def test_windows_default_entry(self):
        devices = [self.Device("Speakers"), self.Device("Default - Headphones")]
        self.assertIs(helper.default_device(devices, False), devices[1])

    def test_elsewhere_the_sound_server_resolves_it(self):
        devices = [self.Device("Monitor of Built-in Audio"), self.Device("Built-in Audio")]
        mic = json.loads(helper.default_device(devices, True).metadata)
        speaker = json.loads(helper.default_device(devices, False).metadata)
        self.assertEqual((mic["id"], mic["is_microphone"]), ("@DEFAULT_SOURCE@", True))
        self.assertEqual((speaker["id"], speaker["is_microphone"]), ("@DEFAULT_SINK@", False))


if __name__ == "__main__":
    unittest.main()
