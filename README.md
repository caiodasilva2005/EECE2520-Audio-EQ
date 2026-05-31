# EECE2520 Audio EQ

This project streams audio through a split-band pipeline:

- Audio processing client reads a file, splits it into low/high bands, and sends frames.
- DAC writer daemon receives paired frames and writes interleaved samples to the IIO DAC device.
- Dash frontend drives the client from a browser: pick a file, play/pause, and adjust the crossover cutoff live with a slider.

## Logic Branch

Use this branch logic for the runtime data path:

```text
START
	|
	v
Client connects to DAC daemon?
	|- No  -> Return error: "Socket unavailable" and stop.
	|- Yes -> Send handshake (sample rate).
						|
						v
			Read next audio block available?
				|- No  -> End stream, close socket.
				|- Yes -> Split into LOW + HIGH bands.
									|
									v
						Daemon has both tags (L and H)?
							|- No  -> Keep buffering pending frame.
							|- Yes -> Enqueue paired frame.
												|
												v
									IIO buffer ready?
										|- No  -> Drop frame and log warning.
										|- Yes -> Scale, interleave, write to /dev/iio:device0.
```

## Frontend

Dash app under `source/frontend/`. Owns an `AudioProcessor` + `AudioClient` and runs `processAudio` on a background thread so the cutoff slider can update the filter live while audio plays.

Setup (one-time):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run on the Pi (DAC daemon present):

```bash
python source/frontend/main.py --samples ./samples
```

Run on a dev machine without the DAC daemon (UI only, no sound):

```bash
python source/frontend/main.py --samples ./samples --dry-run
```

Then open <http://127.0.0.1:8050>. Drop audio files (`.wav`, `.flac`, `.ogg`, `.mp3`, `.aiff`) into `./samples/` and they appear in the dropdown.

### Controls

- **File dropdown** — scans `./samples/` at startup; pick one to load.
- **Play / Pause / Resume** — transport buttons. Filter order is locked to its current value while playing.
- **Cutoff slider** (50–8000 Hz) — updates the Butterworth crossover in real time as you drag.
- **Filter order slider** (1–10) — only applied between songs; changing it mid-play would resize the filter state and race the audio thread.

### Layout

```text
source/
  audio-processing/   AudioProcessor + AudioClient (chunked filter + socket sender)
  dac-writer/         DAC daemon (IIO writer)
  frontend/           Dash UI
    controller.py     PlaybackController — owns processor + client, runs worker thread
    app.py            Dash layout + callbacks
    main.py           argparse entry point
```

## Notes

- The daemon uses bounded queue backpressure to avoid unbounded memory growth.
- Kernel-side IIO timing controls output sample pacing after buffer setup.
- `AudioProcessor` processes audio in blocks specifically so the cutoff can change mid-playback; the frontend builds on top of that contract rather than replacing it.

