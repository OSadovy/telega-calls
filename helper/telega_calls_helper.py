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
       {"ev": "error", "op", "message"}
       {"ev": "stopped"}

Diagnostics go to stderr; `--debug` adds connection state changes.
`--log-dir` keeps them in a file there too, beside NTgCalls' and WebRTC's
logs, in full with `--trace`.  `--protocol` prints the callProtocol to
offer TDLib, `--devices` lists the microphones and speakers.
"""

import argparse
import asyncio
import base64
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


class Device:
    def __init__(self, name, metadata):
        self.name = name
        self.metadata = metadata


def default_device(devices, is_microphone):
    """The system's default microphone or speaker, followed rather than fixed.

    NTgCalls lists Windows' defaults as "Default - " entries, which also
    keeps them out of the Communications role and its turning other audio
    down.  Elsewhere it goes through PulseAudio, which resolves the names
    @DEFAULT_SOURCE@ and @DEFAULT_SINK@ itself; PipeWire's PulseAudio server
    does the same.
    """
    for device in devices:
        if device.name.startswith("Default - "):
            return device
    return Device("default source" if is_microphone else "default sink",
                  json.dumps({"is_microphone": is_microphone,
                              "id": "@DEFAULT_SOURCE@" if is_microphone
                              else "@DEFAULT_SINK@"}))


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


class Call:
    def __init__(self, ntg, mic, speaker):
        self.ntg = ntg
        self.mic = mic
        self.speaker = speaker
        self.user_id = None

    async def start(self, msg):
        state = msg["state"]
        self.user_id = int(msg["user_id"])
        protocol = state.get("protocol") or {}
        await self.ntg.create_p2p_call(self.user_id)
        await self.ntg.set_stream_sources(
            self.user_id, StreamMode.CAPTURE,
            MediaDescription(device_audio(self.mic), None, None, None))
        # Before connect_p2p: NTgCalls opens playback devices again when the
        # call connects, and on Windows a speaker opened a second time blocks
        # that thread for good.  Remote audio is the playback side's
        # "microphone" track, as in py-tgcalls' RecordStream.
        await self.ntg.set_stream_sources(
            self.user_id, StreamMode.PLAYBACK,
            MediaDescription(device_audio(self.speaker), None, None, None))
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


async def serve(mic, speaker):
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

    call = Call(ntg, mic, speaker)
    ntg.on_signaling_data(on_signal)
    ntg.on_connection_change(on_state)
    emit(ev="ready", mic=mic.name, speaker=speaker.name)

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
        for kind in ("microphone", "speaker"):
            for d in getattr(devices, kind):
                emit(kind=kind, name=d.name)
        return
    setup_logging(args.debug, args.log_dir, args.trace)
    mic = (pick(devices.microphone, args.mic) if args.mic
           else default_device(devices.microphone, True))
    asyncio.run(serve(mic, default_device(devices.speaker, False)))


if __name__ == "__main__":
    main()
