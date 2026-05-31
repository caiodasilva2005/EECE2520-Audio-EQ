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

Then open <http://127.0.0.1:8050>.

### Controls

- **File dropdown** — scans `./samples/` at startup; pick one to load.
- **Play / Pause / Resume** — transport buttons. Filter order is locked to its current value while playing.
- **Cutoff slider** (50–8000 Hz) — updates the Butterworth crossover in real time as you drag.
- **Filter order slider** (1–10) — only applied between songs; changing it mid-play would resize the filter state and race the audio thread.
- **Spectrograms** — three live plots (original, low band, high band) refreshed once per second from the most recent ~6 s of audio. Watch the low/high spectrograms split as you drag the cutoff slider.

### Adding audio files (works in dry-run too)

The dropdown reads from whatever directory you pass via `--samples` (default: `./samples` in the repo root). The frontend scans that directory once at startup, so add your files before launching.

1. Create the directory if it doesn't already exist:

   ```bash
   mkdir -p samples
   ```

2. Drop audio files into it. Supported extensions (matched case-insensitively):

   - `.wav`, `.flac`, `.ogg`, `.aiff` / `.aif` — handled natively by `libsndfile`.
   - `.mp3` — also supported by recent `libsndfile`/`soundfile`; if a particular file fails to open, re-encode it to `.wav` as shown below.

3. Quick ways to get something into `samples/`:

   ```bash
   # Use an existing file on your machine
   cp ~/Music/song.wav samples/

   # Or grab a short test tone via ffmpeg (5 s sine sweep, 44.1 kHz mono)
   ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ar 44100 -ac 1 samples/sine440.wav

   # Or convert an unsupported file to .wav
   ffmpeg -i input.m4a -ar 44100 -ac 2 samples/converted.wav
   ```

4. (Optional) point `--samples` somewhere else if you keep audio in a shared folder:

   ```bash
   python source/frontend/main.py --samples ~/Music/eq-tests --dry-run
   ```

5. Restart the app any time you add or remove files — the dropdown is populated on launch, not on every dropdown click.

In dry-run mode the playback runs in real time (the deadline loop still sleeps between blocks), so a 30 s file takes 30 s to scroll through the spectrograms. Pick short clips for fast iteration.

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



## how to enter luca vertual enviorment for this project 

source /Users/lucaspoulos/Python/.venv/bin/activate

