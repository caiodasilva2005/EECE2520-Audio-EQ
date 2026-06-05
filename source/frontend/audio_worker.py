"""Audio engine that runs in its own process.

The real-time DAC streaming must not share a GIL with the Dash web server,
whose per-tick spectrogram FFT + Plotly JSON serialization (~9 MB/s) otherwise
starves the audio thread and causes ~1 s playback stalls. This module holds the
AudioProcessor + AudioClient and runs them in a child process driven entirely by
IPC, so the GUI process can be as busy as it likes without affecting audio.

It deliberately imports only numpy + the audio-processing package (no Dash, no
plotly) so the child process stays lean.

IPC layout (all created by PlaybackController in the parent, passed to the child):
  cmd_q      parent -> child: ('load', path) / ('start',) / ('pause',) /
                              ('resume',) / ('cutoff', hz) / ('seek', block) /
                              ('order', n) / ('quit',)
  status_q   child -> parent: status snapshot dicts, posted on every state change
  ack_q      child -> parent: snapshot posted after a load completes, so the
                              parent's load() can stay synchronous (the upload
                              callback reads the new file's sample rate right away)
  pos        child -> parent: current block index, written cheaply every block
  cap_count  child -> parent: number of captured blocks (ring write counter)
  shm        child -> parent: ring buffer of recent (low, high) blocks for the
                              spectrograms; the parent reads it directly so the
                              audio thread is never blocked by the GUI
"""

import sys
import threading
from pathlib import Path

import numpy as np
from multiprocessing import shared_memory

# Make the sibling audio-processing package importable (its dir name is
# hyphenated, so it can't be imported by name without help).
_AUDIO_DIR = Path(__file__).resolve().parent.parent / "audio-processing"
if str(_AUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(_AUDIO_DIR))

from audio import AudioProcessor                          # noqa: E402
from audioClient import AudioClient, DEFAULT_SOCKET_PATH  # noqa: E402

STATE_IDLE = "idle"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_DONE = "done"
STATE_ERROR = "error"

# Capture ring for the spectrograms. 256 blocks of 1024 samples is ~5.9 s at
# 44.1 kHz — enough context for a readable spectrogram. Two channels per block
# (low band, high band). float32 keeps the shared segment small (~2 MB).
BLOCK_SIZE = 1024
CAPTURE_BLOCKS = 256
CAPTURE_CHANNELS = 2          # 0 = low band, 1 = high band
CAPTURE_SHAPE = (CAPTURE_BLOCKS, CAPTURE_CHANNELS, BLOCK_SIZE)
CAPTURE_DTYPE = np.float32
CAPTURE_NBYTES = int(np.prod(CAPTURE_SHAPE)) * np.dtype(CAPTURE_DTYPE).itemsize


class _AudioEngine:
    """Owns the AudioProcessor/AudioClient inside the child process. Mirrors the
    old in-process controller logic (load/start/pause/resume/stop-on-swap) but
    reports state over IPC instead of returning it."""

    def __init__(self, cmd_q, status_q, ack_q, pos, cap_count, shm_name,
                 socket_path, dry_run, cutoff, order):
        self._cmd_q = cmd_q
        self._status_q = status_q
        self._ack_q = ack_q
        self._pos = pos
        self._cap_count = cap_count
        self._shm = shared_memory.SharedMemory(name=shm_name)
        self._ring = np.ndarray(CAPTURE_SHAPE, dtype=CAPTURE_DTYPE,
                                buffer=self._shm.buf)

        self._socket_path = socket_path
        self._dry_run = dry_run

        # Guards processor/client/thread/state between the command thread (this
        # object's run loop) and the audio worker thread.
        self._lock = threading.Lock()
        self._processor = None
        self._client = None
        self._thread = None

        self._file = None
        self._cutoff = cutoff
        self._order = order
        self._connected = False
        self._state = STATE_IDLE
        self._error = None
        self._total_blocks = 0
        self._sample_rate = 0
        self._duration = 0.0

    # ---- status reporting -------------------------------------------------

    def _snapshot(self):
        # Reads are done without the lock: individual field reads are atomic
        # under the GIL and a momentarily-inconsistent status line self-heals on
        # the next post. This keeps _post_status callable while holding the lock.
        return {
            "state": self._state,
            "file": self._file,
            "cutoff": self._cutoff,
            "order": self._order,
            "connected": self._connected,
            "dry_run": self._dry_run,
            "error": self._error,
            "total_blocks": self._total_blocks,
            "sample_rate": self._sample_rate,
            "duration_seconds": self._duration,
        }

    def _post_status(self):
        self._status_q.put(self._snapshot())

    # ---- capture (runs on the audio thread) -------------------------------

    def _capture_block(self, hf, lf, proc):
        # Single-writer ring: copy the bands into the next slot, then bump the
        # counter. No lock — the parent reads the ring directly and tolerates the
        # rare torn block. Cheap (~a couple of 1024-float copies) so the audio
        # thread is never held up. Cast to float32 to match the shared buffer.
        c = self._cap_count.value
        idx = c % CAPTURE_BLOCKS
        n = min(len(lf), BLOCK_SIZE)
        self._ring[idx, 0, :n] = lf[:n]
        self._ring[idx, 1, :n] = hf[:n]
        if n < BLOCK_SIZE:
            self._ring[idx, 0, n:] = 0.0
            self._ring[idx, 1, n:] = 0.0
        self._cap_count.value = c + 1
        self._pos.value = proc.getCurrentBlock()

    # ---- commands ---------------------------------------------------------

    def _load(self, filepath):
        with self._lock:
            # Stop + disconnect any active worker before swapping: the DAC daemon
            # serves one client at a time and won't accept the next connection
            # until this one closes.
            if self._thread is not None and self._thread.is_alive():
                self._processor.stop()
            if self._client is not None:
                self._client.disconnect()

            try:
                self._processor = AudioProcessor(
                    filepath, cutoffFreq=self._cutoff, filterOrder=self._order,
                )
            except IOError as e:
                self._state = STATE_ERROR
                self._error = str(e)
                self._processor = None
                self._file = None
                self._total_blocks = 0
                self._sample_rate = 0
                self._duration = 0.0
                self._post_status()
                return

            self._file = filepath
            self._client = None
            self._connected = False
            self._thread = None
            self._state = STATE_IDLE
            self._error = None
            self._sample_rate = self._processor.getSamplingFrequency()
            self._total_blocks = self._processor.getTotalBlocks()
            self._duration = self._processor.getDurationSeconds()
            self._pos.value = 0
            self._cap_count.value = 0   # drop the old file's spectrogram data
        self._post_status()

    def _start(self):
        with self._lock:
            needs_reload = self._state in (STATE_DONE, STATE_ERROR) and \
                self._file is not None
            reload_path = self._file if needs_reload else None
        if needs_reload:
            self._load(reload_path)     # re-open at EOF so we can replay

        with self._lock:
            if self._processor is None:
                self._state = STATE_ERROR
                self._error = "No file loaded."
            elif self._state == STATE_PLAYING:
                pass
            elif self._thread is not None and self._thread.is_alive():
                pass                    # paused — use resume()
            else:
                client = AudioClient(self._processor)
                dac_cb = None
                try:
                    client.connectToDacWriterDaemon(self._socket_path)
                    self._connected = True
                    self._client = client
                    dac_cb = client._sendAudioBlockToDaemon
                except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                    if not self._dry_run:
                        self._error = f"Daemon connect failed: {e}"
                        self._state = STATE_ERROR
                    else:
                        # Dry-run: still iterate in real time so position and
                        # spectrograms stay live; you just don't hear anything.
                        self._connected = False
                        self._client = None

                if self._state != STATE_ERROR:
                    proc = self._processor
                    cli = self._client

                    def chained(hf, lf, _dac=dac_cb, _proc=proc):
                        self._capture_block(hf, lf, _proc)
                        if _dac is not None:
                            _dac(hf, lf)

                    proc.setCallback(chained)
                    self._state = STATE_PLAYING
                    self._error = None
                    self._thread = threading.Thread(
                        target=self._run, args=(proc, cli),
                        name="audio-playback", daemon=True,
                    )
                    self._thread.start()
        self._post_status()

    def _run(self, processor, client):
        try:
            processor.processAudio(done_callback=self._on_done)
        except Exception as e:
            # A stop() (file swap) is a clean exit; only a real failure is an
            # error. Skipping the report on the stop path avoids clobbering the
            # new file's state.
            if not processor.isStopped():
                with self._lock:
                    self._state = STATE_ERROR
                    self._error = str(e)
                self._post_status()
        finally:
            if client is not None:
                client.disconnect()

    def _on_done(self):
        with self._lock:
            if self._state != STATE_ERROR:
                self._state = STATE_DONE
        self._post_status()

    def _pause(self):
        with self._lock:
            if self._processor is not None and self._state == STATE_PLAYING:
                self._processor.pause()
                self._state = STATE_PAUSED
        self._post_status()

    def _resume(self):
        with self._lock:
            if self._processor is not None and self._state == STATE_PAUSED:
                self._processor.resume()
                self._state = STATE_PLAYING
        self._post_status()

    def _set_cutoff(self, hz):
        with self._lock:
            self._cutoff = int(hz)
            if self._processor is not None:
                self._processor.setCutoffFrequency(self._cutoff)
        self._post_status()

    def _set_order(self, n):
        with self._lock:
            self._order = int(n)
            if self._state in (STATE_IDLE, STATE_DONE) and \
                    self._processor is not None:
                self._processor.setFilterOrder(self._order)
        self._post_status()

    def _seek(self, block_index):
        with self._lock:
            if self._processor is not None:
                self._processor.seek(int(block_index))
        # No status post: pos updates continuously via the shared value.

    # ---- main loop --------------------------------------------------------

    def run(self):
        self._post_status()             # publish initial idle state
        while True:
            cmd = self._cmd_q.get()
            op = cmd[0]
            if op == "quit":
                break
            elif op == "load":
                self._load(cmd[1])
                self._ack_q.put(self._snapshot())   # unblock parent's load()
            elif op == "start":
                self._start()
            elif op == "pause":
                self._pause()
            elif op == "resume":
                self._resume()
            elif op == "cutoff":
                self._set_cutoff(cmd[1])
            elif op == "order":
                self._set_order(cmd[1])
            elif op == "seek":
                self._seek(cmd[1])

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._processor.stop()
            if self._client is not None:
                self._client.disconnect()
        self._shm.close()


def audio_worker_main(cmd_q, status_q, ack_q, pos, cap_count, shm_name,
                      socket_path, dry_run, cutoff, order):
    """Child-process entry point. Constructed by PlaybackController."""
    engine = _AudioEngine(cmd_q, status_q, ack_q, pos, cap_count, shm_name,
                          socket_path, dry_run, cutoff, order)
    engine.run()
