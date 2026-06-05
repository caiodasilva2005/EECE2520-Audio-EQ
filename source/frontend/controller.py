"""Playback controller.

This is a thin proxy in the web-server process. The actual audio engine
(AudioProcessor + AudioClient) runs in a separate child process (audio_worker)
so the GUI's per-tick spectrogram FFT + Plotly JSON serialization can't share a
GIL with — and therefore can't stall — the real-time DAC streaming. The proxy
just forwards control commands and reads back state/audio over shared memory.

The public API (load/start/pause/resume/set_cutoff/set_position/set_order/
status/recent_audio/sampling_frequency) is unchanged, so the Dash layer is
unaffected by the process split.
"""

import atexit
import queue
import threading
from multiprocessing import get_context, shared_memory

import numpy as np

from audio_worker import (
    audio_worker_main,
    DEFAULT_SOCKET_PATH,
    STATE_IDLE, STATE_PLAYING, STATE_PAUSED, STATE_DONE, STATE_ERROR,
    BLOCK_SIZE, CAPTURE_BLOCKS, CAPTURE_SHAPE, CAPTURE_DTYPE, CAPTURE_NBYTES,
)

# Re-exported so app.py / wsgi.py keep importing these from controller.
__all__ = [
    "PlaybackController", "DEFAULT_SOCKET_PATH",
    "STATE_IDLE", "STATE_PLAYING", "STATE_PAUSED", "STATE_DONE", "STATE_ERROR",
]

_LOAD_TIMEOUT = 10.0   # seconds to wait for the worker to open a new file


class PlaybackController:
    def __init__(self, socket_path=DEFAULT_SOCKET_PATH, dry_run=False,
                 default_cutoff=1000, default_order=10):
        self._dry_run = dry_run

        # 'fork' (Linux default) is used and the worker is spawned here at
        # construction time — which under gunicorn is import time, before any
        # request threads are doing real work — to keep the fork-with-threads
        # hazard minimal while avoiding spawn's __main__ re-import quirks.
        ctx = get_context("fork")
        self._cmd_q = ctx.Queue()
        self._status_q = ctx.Queue()
        self._ack_q = ctx.Queue()
        self._pos = ctx.Value("i", 0)          # current block (written per block)
        self._cap_count = ctx.Value("L", 0)    # capture ring write counter

        # Shared ring for the spectrograms. Created here (parent), attached by
        # name in the child; the parent reads it directly so the audio thread is
        # never blocked by the GUI.
        self._shm = shared_memory.SharedMemory(create=True, size=CAPTURE_NBYTES)
        self._ring = np.ndarray(CAPTURE_SHAPE, dtype=CAPTURE_DTYPE,
                                buffer=self._shm.buf)

        # Local cache of the worker's last-reported status, kept current by a
        # background thread draining status_q. status() reads this plus the live
        # position value, so it never blocks on IPC round-trips.
        self._cache_lock = threading.Lock()
        self._status_cache = {
            "state": STATE_IDLE, "file": None, "cutoff": default_cutoff,
            "order": default_order, "connected": False, "dry_run": dry_run,
            "error": None, "total_blocks": 0, "sample_rate": 0,
            "duration_seconds": 0.0,
        }

        self._proc = ctx.Process(
            target=audio_worker_main,
            args=(self._cmd_q, self._status_q, self._ack_q, self._pos,
                  self._cap_count, self._shm.name, socket_path, dry_run,
                  default_cutoff, default_order),
            name="audio-worker", daemon=True,
        )
        self._proc.start()

        self._reader = threading.Thread(
            target=self._drain_status, name="status-reader", daemon=True)
        self._reader.start()

        self._closed = False
        atexit.register(self.close)

    def _drain_status(self):
        while True:
            snap = self._status_q.get()
            if snap is None:
                return
            with self._cache_lock:
                self._status_cache.update(snap)

    # ---- control (fire-and-forget commands) -------------------------------

    def load(self, filepath):
        # Synchronous: the upload callback reads the new file's sample rate right
        # after, so wait for the worker to finish opening it (or error/timeout).
        self._cmd_q.put(("load", filepath))
        try:
            snap = self._ack_q.get(timeout=_LOAD_TIMEOUT)
            with self._cache_lock:
                self._status_cache.update(snap)
        except queue.Empty:
            pass

    def start(self):
        self._cmd_q.put(("start",))

    def pause(self):
        self._cmd_q.put(("pause",))

    def resume(self):
        self._cmd_q.put(("resume",))

    def set_cutoff(self, hz):
        self._cmd_q.put(("cutoff", int(hz)))

    def set_position(self, block_index):
        self._cmd_q.put(("seek", int(block_index)))

    def set_order(self, n):
        self._cmd_q.put(("order", int(n)))

    # ---- readback ---------------------------------------------------------

    def status(self):
        with self._cache_lock:
            s = dict(self._status_cache)
        sr = s.get("sample_rate") or 0
        pos = self._pos.value
        s["position"] = pos
        s["position_seconds"] = pos * BLOCK_SIZE / sr if sr else 0.0
        s.setdefault("duration_seconds", 0.0)
        return s

    def sampling_frequency(self):
        with self._cache_lock:
            sr = self._status_cache.get("sample_rate") or 0
        return sr or None

    def recent_audio(self):
        """Return (raw, low, high, sample_rate) for the most recent window by
        reading the shared ring directly (no contact with the audio process).
        raw is reconstructed as low + high (the band split is exact)."""
        sr = self.sampling_frequency()
        c = self._cap_count.value
        if c == 0:
            empty = np.empty(0, dtype=np.float32)
            return empty, empty, empty, sr
        n = min(c, CAPTURE_BLOCKS)
        start = c - n
        idx = [(start + k) % CAPTURE_BLOCKS for k in range(n)]
        # Fancy indexing copies, so this is a stable snapshot even as the worker
        # keeps writing; at worst the newest block is momentarily torn, which is
        # invisible in a spectrogram.
        window = self._ring[idx]                       # (n, 2, BLOCK_SIZE)
        lf = window[:, 0, :].reshape(-1)
        hf = window[:, 1, :].reshape(-1)
        raw = lf + hf
        return raw, lf, hf, sr

    # ---- lifecycle --------------------------------------------------------

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._cmd_q.put(("quit",))
        except (ValueError, OSError):
            pass
        if self._proc.is_alive():
            self._proc.join(timeout=2)
            if self._proc.is_alive():
                self._proc.terminate()
        try:
            self._status_q.put(None)       # unblock the reader thread
        except (ValueError, OSError):
            pass
        self._reader.join(timeout=1)
        # Drop each queue's background feeder thread so it can't hold a lock at
        # interpreter shutdown (the source of "_enter_buffered_busy" fatals).
        for q in (self._cmd_q, self._status_q, self._ack_q):
            try:
                q.cancel_join_thread()
                q.close()
            except (ValueError, OSError):
                pass
        try:
            self._shm.close()
            self._shm.unlink()
        except (FileNotFoundError, OSError):
            pass
