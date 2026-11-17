"""Unit tests for the helper's own logic.  Run with the interpreter that has
the requirements installed:  python -m unittest discover helper/tests"""

import base64
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import telega_calls_helper as helper  # noqa: E402

MS = helper.BYTES_PER_MS


def player():
    """A Player with its buffer but no audio device or writer thread."""
    p = helper.Player.__new__(helper.Player)
    p._init_buffer()
    return p


def pcm(ms, value=1000):
    return value.to_bytes(2, "little", signed=True) * (ms * MS // 2)


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


class DefaultMicrophone(unittest.TestCase):
    Device = type("Device", (), {"__init__": lambda self, name: setattr(self, "name", name)})

    def test_windows_default_entry(self):
        devices = [self.Device("Microphone Array"), self.Device("Default - Desktop Microphone")]
        self.assertIs(helper.default_microphone(devices), devices[1])

    def test_elsewhere_the_sound_server_resolves_it(self):
        mic = helper.default_microphone([self.Device("Monitor of Built-in Audio"),
                                         self.Device("Built-in Audio")])
        self.assertEqual(json.loads(mic.metadata)["id"], "@DEFAULT_SOURCE@")


class PlayerBuffer(unittest.TestCase):
    def test_silence_until_target_held(self):
        p = player()
        p.feed(pcm(helper.TARGET_BUFFER_MS - 10))
        self.assertEqual(p.next_chunk(10 * MS), bytes(10 * MS))
        p.feed(pcm(10))
        self.assertEqual(p.next_chunk(10 * MS), pcm(10))
        self.assertEqual(p.delivered, 10 * MS)

    def test_running_dry_refills_to_target(self):
        p = player()
        p.feed(pcm(helper.TARGET_BUFFER_MS))
        for _ in range(helper.TARGET_BUFFER_MS // 10):
            p.next_chunk(10 * MS)
        self.assertEqual(p.next_chunk(10 * MS), bytes(10 * MS))
        self.assertEqual(p.stats["starved"], 1)
        p.feed(pcm(10))
        self.assertEqual(p.next_chunk(10 * MS), bytes(10 * MS),
                         "plays silence until the target is held again")

    def test_overflow_drops_oldest_down_to_target(self):
        p = player()
        p.feed(pcm(helper.MAX_BUFFER_MS, value=1))
        p.feed(pcm(10, value=2))
        self.assertEqual(len(p.buffer), helper.TARGET_BUFFER_MS * MS)
        self.assertEqual(p.buffer[-10 * MS:], pcm(10, value=2))
        self.assertEqual(p.stats["dropped"], (helper.MAX_BUFFER_MS + 10 - helper.TARGET_BUFFER_MS) * MS)

    def test_report_resets_and_measures_peak(self):
        p = player()
        p.feed(pcm(10, value=-1234))
        self.assertIn("peak 1234", p.report())
        self.assertIn("peak 0", p.report())


class FakeSounddevice:
    """Stands in for sounddevice: the default output is whichever device
    `default_output` names, and a stream's writes fail once its device is
    no longer the default, as when headphones are unplugged."""

    def __init__(self):
        self.default_output = 1
        self.restarts = 0
        self.written = []
        self.lock = threading.Lock()
        fake = self

        class Stream:
            def __init__(self, device, **_):
                self.device = device
                self.latency = 0.02

            def start(self):
                if self.device is None:
                    raise RuntimeError("no device")

            def write(self, chunk):
                if self.device != fake.default_output:
                    raise RuntimeError("device invalidated")
                with fake.lock:
                    fake.written.append((self.device, chunk))
                time.sleep(0.001)
                return False

            def close(self, ignore_errors=True):
                pass

        self.RawOutputStream = Stream

    @property
    def default(self):
        return type("Default", (), {"device": (None, self.default_output)})()

    def query_hostapis(self):
        return []

    def query_devices(self, device):
        return {"name": f"device {device}"}

    def _terminate(self):
        pass

    def _initialize(self):
        self.restarts += 1


class PlayerOutputLoss(unittest.TestCase):
    def setUp(self):
        self.saved = helper.sounddevice
        helper.sounddevice = self.sd = FakeSounddevice()
        self.addCleanup(setattr, helper, "sounddevice", self.saved)

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.01)

    def test_moves_to_the_new_default_and_keeps_playing(self):
        moved = []
        p = helper.Player(on_output=moved.append)
        self.addCleanup(p.close)
        self.wait_for(lambda: self.sd.written)
        self.sd.default_output = 2
        self.wait_for(lambda: moved)
        self.assertEqual(moved, ["device 2"])
        self.assertEqual(self.sd.restarts, 1)
        p.feed(pcm(helper.TARGET_BUFFER_MS, value=7))
        self.wait_for(lambda: any(d == 2 and c == pcm(10, value=7) for d, c in self.sd.written))

    def test_waits_while_there_is_no_output(self):
        moved = []
        p = helper.Player(on_output=moved.append)
        self.addCleanup(p.close)
        p.RETRY_SECONDS = 0.01
        self.sd.default_output = None
        self.wait_for(lambda: self.sd.restarts >= 2)
        self.assertEqual(moved, [])
        self.sd.default_output = 3
        self.wait_for(lambda: moved)
        self.assertEqual(moved, ["device 3"])


if __name__ == "__main__":
    unittest.main()
