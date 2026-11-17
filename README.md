# telega-calls

Voice calls for [telega.el](https://github.com/zevlg/telega.el), with the
audio carried by a separate helper program.

telega already places, answers and hangs up private calls through TDLib.
What it lacks is a working media engine: the one it can be built with,
libtgvoip, no longer interoperates with current Telegram apps.
`telega-calls-mode` hands each call's media to a helper instead and relays
the call's signalling between the helper and TDLib.

The bundled helper is Python on [NTgCalls](https://github.com/pytgcalls/ntgcalls).
It is used on Windows, from Emacs under WSL2 with a Windows Python, so the
audio is Windows'. It should also run on Linux with PulseAudio or PipeWire,
but has not carried a real call there yet. Nothing in the Emacs side is tied
to either: any program speaking the JSON-lines protocol at the top of
`helper/telega_calls_helper.py` can replace the helper.

## Setup

1. A Python, 3.10 or later, with the helper's requirements in a virtual
   environment.

   On Windows, used from WSL:

   ```
   py -3.14 -m venv C:\Users\me\telega-calls\venv
   C:\Users\me\telega-calls\venv\Scripts\python -m pip install -r helper\requirements.txt
   ```

   On Linux the helper needs a PulseAudio server, which PipeWire provides
   through `pipewire-pulse`:

   ```
   python3 -m venv ~/.local/share/telega-calls/venv
   ~/.local/share/telega-calls/venv/bin/pip install -r helper/requirements.txt
   ```

2. In Emacs, with `telega-calls-python` naming that environment's Python:

   ```elisp
   (add-to-list 'load-path "/path/to/telega-calls")
   (with-eval-after-load 'telega
     (require 'telega-calls)
     (setq telega-calls-python "/mnt/c/Users/me/telega-calls/venv/Scripts/python.exe")
     (telega-calls-mode 1))
   ```

   A Windows Python is recognised by its `.exe`, and is handed Windows
   paths.

## Use

telega's call keys, under `c` in the root buffer:

| key | command |
|---|---|
| `c c` | call the chat at point (`telega-chat-call` in a chat) |
| `c a` | answer |
| `c d` | hang up or decline |
| `c m` | mute or unmute the microphone (`telega-calls-toggle-mute`) |

Progress is reported in the echo area ("Call audio connected") and logged
in `*telega-calls*`.

## Settings

- `telega-calls-microphone`: part of a microphone's name, as the helper's
  `--devices` lists them. Unset, each call uses the system default.
- Playback always goes to the system's default output. If a device in use
  goes away during a call, NTgCalls reopens the default there is then.
- `telega-calls-debug`: log the helper's debugging messages, such as its
  connection state, in `*telega-calls*`.
- `telega-calls-log-directory`, `telega-calls-trace`: keep NTgCalls' and
  WebRTC's logs, one file per call. They contain connection details.

## Limitations

- Private calls only; group voice chats are not handled.
- No echo cancellation of its own; headphones avoid the question. On
  Windows, NTgCalls opens the microphone as a Communications stream with
  system processing allowed, so whatever the device offers (Voice Clarity,
  a vendor's effects) applies; it is set in Windows under the microphone's
  Audio enhancements.

  On Linux nothing in the recording path cancels echo. PipeWire's
  echo-cancel module, which uses WebRTC's canceller, adds a virtual source
  and sink: audio played into the sink is removed from what the source
  records. Load it as its documentation describes (a drop-in under
  `~/.config/pipewire/pipewire.conf.d/`), and make that source and sink the
  default input and output. The helper records from the default input and
  plays to the default output, so it uses them with no setting of its own.
  PulseAudio's `module-echo-cancel` works the same way. Not yet tried with
  telega-calls.

## Tests

```
emacs --batch --eval "(setq load-prefer-newer t)" -f package-initialize -L . -l test/telega-calls-tests.el -f ert-run-tests-batch-and-exit
python -m unittest discover helper/tests        # with the helper's Python
python helper/tests/manual_loopback.py          # by hand: needs audio devices
```
