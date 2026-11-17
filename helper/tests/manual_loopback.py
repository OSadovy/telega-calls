"""The helper's Call, as caller, against a bare NTgCalls callee in the same
process, with no Telegram involved: both take the same random key, and an
in-memory relay carries their signalling.

Needs real audio devices, so it is run by hand:  the callee sends the
default microphone, and the Call plays it on the default output for
SECONDS (default 8).  Expect both peers CONNECTED, and to hear the
microphone.
"""

import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ntgcalls import MediaDescription, NTgCalls, StreamMode  # noqa: E402

import telega_calls_helper as helper  # noqa: E402


async def main(seconds):
    loop = asyncio.get_running_loop()
    ntg = NTgCalls()
    devices = NTgCalls.get_media_devices()
    mic = helper.default_device(devices.microphone, True)
    call = helper.Call(ntg, mic, helper.default_device(devices.speaker, False))
    connected = {}
    done = asyncio.Event()

    def on_signal(peer, data):
        other = 2 if peer == 1 else 1
        # NTgCalls makes its future on the calling thread, which needs a loop.
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(ntg.send_signaling_data(other, data)))

    def on_state(peer, info):
        print(f"peer {peer}: {info.state.name}", flush=True)
        if info.state.name != "CONNECTING":
            connected[peer] = info.state.name
            if len(connected) == 2:
                loop.call_soon_threadsafe(done.set)

    ntg.on_signaling_data(on_signal)
    ntg.on_connection_change(on_state)

    key = os.urandom(256)
    await ntg.create_p2p_call(2)
    await ntg.set_stream_sources(2, StreamMode.CAPTURE,
                                 MediaDescription(helper.device_audio(mic), None, None, None))
    await ntg.skip_exchange(2, key, False)
    await call.start({"op": "start", "user_id": 1, "is_outgoing": True,
                      "state": {"protocol": {"library_versions": ["8.0.0"]},
                                "servers": [], "allow_p2p": True,
                                "custom_parameters": "",
                                "encryption_key": base64.b64encode(key).decode()}})
    await ntg.connect_p2p(2, [], ["8.0.0"], True, None)
    try:
        await asyncio.wait_for(done.wait(), 15)
        await asyncio.sleep(seconds)
    except asyncio.TimeoutError:
        print("timed out", flush=True)
    print(f"result: {connected}", flush=True)
    await call.stop()
    os._exit(0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    helper.setup_logging(debug=True)
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 8))
