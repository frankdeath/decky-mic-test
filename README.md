# Mic Test

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin for
Steam Deck and Steam Machine that helps you check and configure audio input and
output devices.

## Features

- Choose the default audio output and adjust its volume or mute it.
- Choose the default audio input and adjust its volume or mute it.
- Live input level meter with peak and RMS readings in dBFS. The meter stops
  when the Quick Access Menu is closed.
- Record a test clip from the selected microphone and play it back through the
  selected output. Closing the Quick Access Menu cancels an active recording.
- Replay the last test clip without recording again.
- Device lists refresh automatically when devices are plugged in or removed.
- Runs as the SteamOS session user, so it can access the active PipeWire
  instance.

## Requirements

- SteamOS on a Steam Deck or Steam Machine.
- Decky Loader.
- The PulseAudio compatibility tools shipped with SteamOS (`pactl`, `parec`,
  `paplay`). PipeWire equivalents `pw-record`/`pw-play` are used as fallbacks.

## Building

Install Node.js and pnpm, then run:

```bash
pnpm install
pnpm run build
```

The bundled frontend is written to `dist/index.js`. Build a deployable plugin
zip with the Decky CLI or the VS Code `build` task from the plugin template.

## Installing

Install the resulting zip through Decky Loader's developer settings, or copy the
plugin directory into Decky Loader's `plugins` directory.

## Using the plugin

1. Open the Quick Access Menu and select the Mic Test tab.
2. Pick an output device and set its volume.
3. Pick an input device and set its volume.
4. Speak into the microphone and watch the live level meter.
5. Press **Start test recording**, speak, then press **Stop & play back**. The
   clip plays through the selected output.

If the meter does not move, check that the input is unmuted and that the correct
microphone is selected.

## Development notes

Audio control is handled by the Python backend in `main.py` using `pactl`.
Recording is captured as 48 kHz mono 16-bit PCM, converted to WAV with Python's
`wave` module, and measured with no external Python dependencies.

The plugin intentionally does not use the `_root` flag. Running as the session
user is required for access to that user's PipeWire session.

## License

BSD-3-Clause. The original Decky plugin template license is retained in
`LICENSE`.
