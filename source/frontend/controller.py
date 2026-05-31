"""Playback controller that owns an AudioProcessor + AudioClient and runs
processAudio on a worker thread so Dash callbacks can poke the processor
(slider drag, pause/resume) without blocking the web event loop.
"""

import sys
import threading
from pathlib import Path

# Make the sibling audio-processing package importable. The directory name has
# a hyphen, so a normal `import` won't work; sys.path is the simplest fix
# without restructuring the user's existing layout.
_AUDIO_DIR = Path(__file__).resolve().parent.parent / "audio-processing"
sys.path.insert(0, str(_AUDIO_DIR))

from audio import AudioProcessor                      # noqa: E402
from audioClient import AudioClient, DEFAULT_SOCKET_PATH  # noqa: E402


STATE_IDLE = "idle"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_DONE = "done"
STATE_ERROR = "error"


class PlaybackController:
    def __init__(self, socket_path=DEFAULT_SOCKET_PATH, dry_run=False,
                 default_cutoff=1000, default_order=10):
        self._socket_path = socket_path
        self._dry_run = dry_run
        self._lock = threading.Lock()

        self._processor = None
        self._client = None
        self._thread = None

        self._current_file = None
        self._cutoff = default_cutoff
        self._order = default_order
        self._connected = False
        self._state = STATE_IDLE
        self._error = None

    # The audio thread can outlive a "stop" because AudioProcessor has no stop
    # primitive — pausing it just parks it on a threading.Event. We let the
    # old worker linger as a daemon thread; it dies with the process. Loading
    # a new file drops the reference and the new processor takes over.
    def load(self, filepath):
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and \
                    self._state == STATE_PLAYING:
                # Park the old worker before swapping. It'll sit on the
                # pause event for the rest of the process lifetime.
                self._processor.pause()

            try:
                self._processor = AudioProcessor(
                    filepath,
                    cutoffFreq=self._cutoff,
                    filterOrder=self._order,
                )
            except IOError as e:
                self._state = STATE_ERROR
                self._error = str(e)
                self._processor = None
                self._current_file = None
                return

            self._current_file = filepath
            self._client = None
            self._connected = False
            self._thread = None
            self._state = STATE_IDLE
            self._error = None

    def start(self):
        with self._lock:
            if self._processor is None:
                self._error = "No file loaded."
                self._state = STATE_ERROR
                return
            if self._state == STATE_PLAYING:
                return
            if self._thread is not None and self._thread.is_alive():
                # Could be paused — caller should use resume() instead.
                return

            client = AudioClient(self._processor)
            try:
                client.connectToDacWriterDaemon(self._socket_path)
                self._connected = True
                self._client = client
            except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                if not self._dry_run:
                    self._error = f"Daemon connect failed: {e}"
                    self._state = STATE_ERROR
                    return
                # Dry-run: skip the socket and install a no-op callback so
                # AudioProcessor still iterates blocks in real time. The
                # slider remains live; you just don't hear anything.
                self._processor.setCallback(lambda hf, lf: None)
                self._connected = False
                self._client = None

            self._state = STATE_PLAYING
            self._error = None
            self._thread = threading.Thread(
                target=self._run, name="audio-playback", daemon=True,
            )
            self._thread.start()

    def _run(self):
        try:
            self._processor.processAudio(done_callback=self._on_done)
        except Exception as e:
            with self._lock:
                self._state = STATE_ERROR
                self._error = str(e)

    def _on_done(self):
        with self._lock:
            if self._state != STATE_ERROR:
                self._state = STATE_DONE

    def pause(self):
        with self._lock:
            if self._processor is not None and self._state == STATE_PLAYING:
                self._processor.pause()
                self._state = STATE_PAUSED

    def resume(self):
        with self._lock:
            if self._processor is not None and self._state == STATE_PAUSED:
                self._processor.resume()
                self._state = STATE_PLAYING

    def set_cutoff(self, hz):
        # Cheap and safe to call mid-playback: AudioProcessor.setCutoffFrequency
        # rebuilds the SOS and zeros the filter state. The number of sections
        # is unchanged (filter order is fixed during playback), so the shapes
        # the audio thread reads always match.
        with self._lock:
            self._cutoff = int(hz)
            if self._processor is not None:
                self._processor.setCutoffFrequency(self._cutoff)

    def set_order(self, n):
        # Changing order changes SOS shape and can race the audio thread's
        # sosfilt call. Only apply when nothing is actively playing; the UI
        # should disable this control during playback.
        with self._lock:
            self._order = int(n)
            if self._state in (STATE_IDLE, STATE_DONE) and self._processor is not None:
                self._processor.setFilterOrder(self._order)

    def status(self):
        with self._lock:
            return {
                "state": self._state,
                "file": self._current_file,
                "cutoff": self._cutoff,
                "order": self._order,
                "connected": self._connected,
                "dry_run": self._dry_run,
                "error": self._error,
            }
