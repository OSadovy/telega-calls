"""Carry the audio of one Telegram private call that telega set up.

TDLib, inside telega, does the call signalling and key exchange; this
process runs only the media, through NTgCalls.  Emacs writes JSON lines on
stdin and reads JSON lines on stdout:

  in:  {"op": "start", "user_id", "is_outgoing", "state"}  (TDLib callStateReady)
       {"op": "signal", "data": base64}   (updateNewCallSignalingData)
       {"op": "mute"} {"op": "unmute"} {"op": "stop"}
  out: {"ev": "ready", "mic", "speaker"}
       {"ev": "signal", "data": base64}   (for sendCallSignalingData)
       {"ev": "state", "state", "kind"}
       {"ev": "output", "name"}           (playback moved to another device)
       {"ev": "error", "op", "message"}
       {"ev": "stopped"}

Diagnostics go to stderr; `--debug` adds playback statistics every five
seconds and at the end of the call.  `--log-dir` keeps them in a file there
too, beside NTgCalls' and WebRTC's logs, in full with `--trace`.
`--protocol` prints the callProtocol to offer TDLib, `--devices` lists the
microphones.
"""

import argparse
import asyncio
import base64
import importlib
import json
import logging
import os
import sys
import threading
import time

from ntgcalls import (
    AudioDescription,
    MediaDescription,
    MediaSource,
    NTgCalls,
    RTCServer,
    StreamMode,
)

# py-tgcalls' default (AudioQuality.HIGH).
SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_MS = SAMPLE_RATE * CHANNELS * 2 // 1000

# Playback: the output latency asked of the device, the received audio held
# before playing starts or resumes after running dry, and the most held
# before the oldest is dropped back down to the target.
OUTPUT_LATENCY = 0.01
TARGET_BUFFER_MS = 20
MAX_BUFFER_MS = 50

log = logging.getLogger("telega_calls")
_out_lock = threading.Lock()


def emit(**msg):
    line = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
    with _out_lock:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()


def setup_logging(debug, log_dir=None, trace=False):
    """The helper's messages to stderr, as they are; with LOG_DIR, to a file
    as well, timestamped, beside NTgCalls' and WebRTC's own logs, which are
    off unless a logger is configured for them."""
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(stderr)
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    if not log_dir:
        return
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, time.strftime("call-%Y%m%d-%H%M%S.log"))
    file = logging.FileHandler(path, encoding="utf-8")
    file.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s", "%H:%M:%S"))
    log.addHandler(file)
    for name, level in (("ntgcalls", logging.DEBUG if trace else logging.INFO),
                        ("webrtc", logging.DEBUG if trace else logging.WARNING)):
        logging.getLogger(name).setLevel(level)
        logging.getLogger(name).addHandler(file)
    log.info("log file: %s", path)


def pick(devices, needle):
    for device in devices:
        if needle.lower() in device.name.lower():
            return device
    names = ", ".join(d.name for d in devices)
    raise SystemExit(f"no device matching {needle!r} among: {names}")


class Microphone:
    def __init__(self, name, metadata):
        self.name = name
        self.metadata = metadata


def default_microphone(devices):
    """The system's default microphone, followed rather than fixed.

    NTgCalls lists Windows' default as a "Default - " entry, which also
    keeps it out of the Communications role and its turning other audio
    down.  Elsewhere it records through PulseAudio, which resolves the name
    @DEFAULT_SOURCE@ itself; PipeWire's PulseAudio server does the same.
    """
    for device in devices:
        if device.name.startswith("Default - "):
            return device
    return Microphone("default source",
                      json.dumps({"is_microphone": True, "id": "@DEFAULT_SOURCE@"}))


def b64(data):
    return base64.b64decode(data) if data else b""


def rtc_servers(servers):
    """Map TDLib callServer objects to NTgCalls', as py-tgcalls maps MTProto's."""
    result = []
    for s in servers:
        kind = s.get("type") or {}
        common = (int(s["id"]), s.get("ip_address") or "",
                  s.get("ipv6_address") or "", int(s["port"]))
        if kind.get("@type") == "callServerTypeTelegramReflector":
            result.append(RTCServer(*common, None, None, True, False,
                                    bool(kind.get("is_tcp")),
                                    b64(kind.get("peer_tag")) or None))
        else:
            result.append(RTCServer(*common, kind.get("username") or None,
                                    kind.get("password") or None,
                                    bool(kind.get("supports_turn")),
                                    bool(kind.get("supports_stun")),
                                    False, None))
    return result


def device_audio(device):
    return AudioDescription(MediaSource.DEVICE, SAMPLE_RATE, CHANNELS,
                            device.metadata, False)


def import_sounddevice():
    """Import sounddevice on a thread of its own.

    On Windows PortAudio initialises COM on the importing thread in a mode
    that makes NTgCalls' device calls on that thread abort the process.
    """
    loaded = {}
    thread = threading.Thread(
        target=lambda: loaded.update(module=importlib.import_module("sounddevice")))
    thread.start()
    thread.join()
    return loaded["module"]


sounddevice = None


def output_device():
    """The default output, through WASAPI where there is one.

    MME, PortAudio's default on Windows, adds about 90 ms.
    """
    for api in sounddevice.query_hostapis():
        if api["name"] == "Windows WASAPI":
            return api["default_output_device"], sounddevice.WasapiSettings(auto_convert=True)
    return sounddevice.default.device[1], None


class Player:
    """Play the remote audio, which NTgCalls hands over as 10 ms PCM frames.

    NTgCalls' own Windows output (a DEVICE playback source) stalls its
    callback thread, so the call never connects; the frames are taken as
    EXTERNAL instead and played here, by a thread writing to the stream:
    PortAudio's WASAPI stream driven by a callback can play nothing.

    ON_OUTPUT is called with the device's name when playback has to move to
    another one.
    """

    CHUNK = 10 * BYTES_PER_MS
    RETRY_SECONDS = 1

    def __init__(self, on_output=None):
        global sounddevice
        sounddevice = sounddevice or import_sounddevice()
        self._init_buffer()
        self.on_output = on_output or (lambda _name: None)
        self._open()
        self.stopping = False
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.writer.start()

    def _open(self):
        device, extra = output_device()
        stream = sounddevice.RawOutputStream(
            device=device, samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="int16", latency=OUTPUT_LATENCY, extra_settings=extra)
        stream.start()
        self.stream = stream
        self.device_name = sounddevice.query_devices(device)["name"]

    def _reopen(self):
        """Move playback to the current default output after a write failed.

        PortAudio reads the device list once, when initialised, so it is
        restarted to see the devices present now.
        """
        try:
            self.stream.close(ignore_errors=True)
        except Exception:
            pass
        while not self.stopping:
            try:
                sounddevice._terminate()
                sounddevice._initialize()
                self._open()
            except Exception as e:
                log.warning("no output to move to: %s: %s", type(e).__name__, e)
                time.sleep(self.RETRY_SECONDS)
                continue
            log.info("playback moved to %s", self.device_name)
            self.on_output(self.device_name)
            return

    def _init_buffer(self):
        self.buffer = bytearray()
        self.filling = True
        self.delivered = 0
        self.stats = self._new_stats()
        self.lock = threading.Lock()

    @staticmethod
    def _new_stats():
        return {"chunks": 0, "level_sum": 0, "level_min": None, "level_max": 0,
                "starved": 0, "underflows": 0, "dropped": 0, "peak": 0}

    def feed(self, pcm):
        samples = memoryview(pcm).cast("h")
        peak = max(max(samples), -min(samples)) if samples else 0
        with self.lock:
            self.stats["peak"] = max(self.stats["peak"], peak)
            self.buffer += pcm
            if len(self.buffer) > MAX_BUFFER_MS * BYTES_PER_MS:
                drop = len(self.buffer) - TARGET_BUFFER_MS * BYTES_PER_MS
                del self.buffer[:drop]
                self.stats["dropped"] += drop

    def _write(self):
        while not self.stopping:
            chunk = self.next_chunk(self.CHUNK)
            try:
                underflowed = self.stream.write(chunk)
            except Exception as e:
                log.warning("output failed: %s: %s", type(e).__name__, e)
                self._reopen()
                continue
            if underflowed:
                with self.lock:
                    self.stats["underflows"] += 1

    def next_chunk(self, n):
        with self.lock:
            stats = self.stats
            level = len(self.buffer)
            stats["chunks"] += 1
            stats["level_sum"] += level
            stats["level_max"] = max(stats["level_max"], level)
            if stats["level_min"] is None or level < stats["level_min"]:
                stats["level_min"] = level
            if self.filling and level >= TARGET_BUFFER_MS * BYTES_PER_MS:
                self.filling = False
            if not self.filling and level < n:
                # Ran dry: play silence until the target is held again
                # rather than a click every time a frame comes late.
                stats["starved"] += 1
                self.filling = True
            if self.filling:
                return bytes(n)
            chunk = bytes(self.buffer[:n])
            del self.buffer[:n]
            self.delivered += n
            return chunk

    def report(self):
        """Describe playback since the last report."""
        with self.lock:
            stats, self.stats = self.stats, self._new_stats()
        chunks = stats["chunks"] or 1
        ms = lambda b: (b or 0) / BYTES_PER_MS
        return (f"playback: buffer avg {ms(stats['level_sum'] / chunks):.0f} ms "
                f"(min {ms(stats['level_min']):.0f}, max {ms(stats['level_max']):.0f}), "
                f"starved {stats['starved']}, device underflows {stats['underflows']}, "
                f"dropped {ms(stats['dropped']):.0f} ms, peak {stats['peak']}")

    def close(self):
        self.stopping = True
        self.writer.join()
        self.stream.close(ignore_errors=True)


class Call:
    def __init__(self, ntg, mic):
        self.ntg = ntg
        self.mic = mic
        self.user_id = None
        self.player = None
        self.reporter = None

    def on_frames(self, _id, mode, _device, frames):
        if mode == StreamMode.PLAYBACK and self.player:
            for frame in frames:
                self.player.feed(frame.data)

    async def start(self, msg):
        state = msg["state"]
        self.user_id = int(msg["user_id"])
        protocol = state.get("protocol") or {}
        await self.ntg.create_p2p_call(self.user_id)
        await self.ntg.set_stream_sources(
            self.user_id, StreamMode.CAPTURE,
            MediaDescription(device_audio(self.mic), None, None, None))
        await self.ntg.skip_exchange(self.user_id,
                                     b64(state["encryption_key"]),
                                     bool(msg.get("is_outgoing")))
        servers = rtc_servers(state.get("servers") or [])
        log.info("connecting: outgoing %s, versions %s, p2p %s, %d servers",
                 bool(msg.get("is_outgoing")), protocol.get("library_versions"),
                 bool(state.get("allow_p2p")), len(servers))
        await self.ntg.connect_p2p(self.user_id, servers,
                                   list(protocol.get("library_versions") or []),
                                   bool(state.get("allow_p2p")),
                                   state.get("custom_parameters") or None)
        # Remote audio is the playback side's "microphone" track, as in
        # py-tgcalls' RecordStream.
        self.player = Player(on_output=lambda name: emit(ev="output", name=name))
        log.info("playing on %s, output latency %.0f ms",
                 self.player.device_name, self.player.stream.latency * 1000)
        if log.isEnabledFor(logging.DEBUG):
            self.reporter = asyncio.create_task(self.report_playback())
        await self.ntg.set_stream_sources(
            self.user_id, StreamMode.PLAYBACK,
            MediaDescription(AudioDescription(MediaSource.EXTERNAL, SAMPLE_RATE,
                                              CHANNELS, "", False),
                             None, None, None))

    async def report_playback(self):
        while True:
            await asyncio.sleep(5)
            log.debug("%s", self.player.report())

    async def handle(self, msg):
        op = msg["op"]
        if op == "start":
            await self.start(msg)
        elif op == "signal":
            if self.user_id is None:
                log.warning("signal dropped: call not started")
                return
            await self.ntg.send_signaling_data(self.user_id, b64(msg["data"]))
        elif op == "mute":
            await self.ntg.mute(self.user_id)
        elif op == "unmute":
            await self.ntg.unmute(self.user_id)
        else:
            raise ValueError(f"unknown op {op!r}")

    async def stop(self):
        if self.user_id is not None:
            try:
                await asyncio.wait_for(self.ntg.stop(self.user_id), 2)
            except Exception as e:
                log.warning("stop: %s: %s", type(e).__name__, e)
        if self.reporter:
            self.reporter.cancel()
        if self.player:
            self.player.close()
            if log.isEnabledFor(logging.DEBUG):
                log.debug("%s", self.player.report())
                log.debug("played %.1f s of remote audio",
                          self.player.delivered / (BYTES_PER_MS * 1000))


async def serve(mic):
    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()

    def read_stdin():
        for line in sys.stdin.buffer:
            loop.call_soon_threadsafe(lines.put_nowait, line)
        loop.call_soon_threadsafe(lines.put_nowait, None)

    threading.Thread(target=read_stdin, daemon=True).start()

    ntg = NTgCalls()

    def on_signal(_id, data):
        emit(ev="signal", data=base64.b64encode(data).decode("ascii"))

    def on_state(_id, info):
        log.debug("connection %s", info.state.name)
        emit(ev="state", state=info.state.name, kind=info.kind.name)

    call = Call(ntg, mic)
    ntg.on_signaling_data(on_signal)
    ntg.on_connection_change(on_state)
    ntg.on_frames(call.on_frames)
    global sounddevice
    sounddevice = import_sounddevice()
    emit(ev="ready", mic=mic.name,
         speaker=sounddevice.query_devices(output_device()[0])["name"])

    # One message at a time, from the loop's thread, which NTgCalls needs
    # to make its futures on; it buffers signalling that arrives after
    # create_p2p_call until the connection exists.
    while (line := await lines.get()) is not None:
        msg = json.loads(line)
        if msg.get("op") == "stop":
            break
        try:
            await call.handle(msg)
        except Exception as e:
            emit(ev="error", op=msg.get("op"), message=f"{type(e).__name__}: {e}")
    await call.stop()
    emit(ev="stopped")
    # The stdin reader thread still holds stdin's lock; a normal
    # interpreter shutdown aborts on it.
    sys.stderr.flush()
    os._exit(0)


def main():
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", action="store_true")
    parser.add_argument("--devices", action="store_true")
    parser.add_argument("--mic", help="part of a microphone's name; "
                        "the system default if omitted")
    parser.add_argument("--log-dir")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.protocol:
        p = NTgCalls.get_protocol()
        emit(min_layer=p.min_layer, max_layer=p.max_layer,
             udp_p2p=p.udp_p2p, udp_reflector=p.udp_reflector,
             library_versions=list(p.library_versions))
        return
    devices = NTgCalls.get_media_devices()
    if args.devices:
        for d in devices.microphone:
            emit(kind="microphone", name=d.name)
        return
    setup_logging(args.debug, args.log_dir, args.trace)
    mic = (pick(devices.microphone, args.mic) if args.mic
           else default_microphone(devices.microphone))
    asyncio.run(serve(mic))


if __name__ == "__main__":
    main()
