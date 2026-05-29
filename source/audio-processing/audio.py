
import time
import threading
import numpy as np
from soundfile import SoundFile
from scipy import signal as sig

# An Audio Processor divide an audio file into two frequency channels
# based on the cuoff frequency and filter order.
# It process the file in blocks and performs a given callback function on each channel
# after processing each block
class AudioProcessor:

    # constructs a AudioProcessor object with the given parameters
    # param filepath: the path to the audio file to be processed
    # param callback: a function that takes two arguments (highFrequencyChannel, lowFrequencyChannel
    #                 and is called after processing each block of audio data
    # param cutoffFreq: the cutoff frequency for the high-pass filter (default: 1000 Hz)
    # param filterOrder: the order of the Butterworth filter (default: 10)
    # param blockSize: the size of each audio block to be processed (default
    #                    1024 samples)
    # throws IOError if there is an error reading the audio file
    def __init__(self, filepath, cutoffFreq=1000, filterOrder=10, blockSize=1024):
        self._filepath = filepath
        self._cutoffFreq = cutoffFreq
        self._filterOrder = filterOrder
        self._callback = None
        self._blockSize = blockSize

        self._highFrequencyChannel = np.zeros(blockSize)
        self._LowFrequencyChannel = np.zeros(blockSize)

        try:
            self._soundFile = SoundFile(filepath)
        except Exception as e:
            raise IOError(f"Error reading audio file: {e}")

        self._sos = self._buildSos()
        self._zi = np.zeros((self._sos.shape[0], 2))

        # Event is set when running, cleared when paused.
        self._pauseEvent = threading.Event()
        self._pauseEvent.set()

    # builds the second-order sections for the Butterworth high-pass filter based on the cutoff frequency and filter order
    def _buildSos(self):
        return sig.butter(self._filterOrder, self._cutoffFreq, 'hp',
                          fs=self._soundFile.samplerate, output='sos')

    # filters the given audio block using the high-pass filter and separates it into high-frequency and low-frequency channels
    def _filterAudioBlock(self, audioBlock):
        t0 = time.monotonic()
        self._highFrequencyChannel, self._zi = sig.sosfilt(self._sos, audioBlock, axis=0, zi=self._zi)
        self._LowFrequencyChannel = audioBlock - self._highFrequencyChannel
        self._lastFilterMs = (time.monotonic() - t0) * 1000.0

    # sets the filter order and rebuilds the second-order sections for the Butterworth high-pass filter
    def setFilterOrder(self, filterOrder):
        self._filterOrder = filterOrder
        self._sos = self._buildSos()
        self._zi = np.zeros((self._sos.shape[0], 2))

    # sets the cutoff frequency and rebuilds the second-order sections for the Butterworth high-pass filter
    def setCutoffFrequency(self, cutoffFreq):
        self._cutoffFreq = cutoffFreq
        self._sos = self._buildSos()
        self._zi = np.zeros((self._sos.shape[0], 2))

    def setCallback(self, callback):
        self._callback = callback

    # pauses processAudio; safe to call from another thread
    def pause(self):
        self._pauseEvent.clear()

    # resumes processAudio after a pause; safe to call from another thread
    def resume(self):
        self._pauseEvent.set()

    def isPaused(self):
        return not self._pauseEvent.is_set()

    # gets the sampling frequency of the audio file
    def getSamplingFrequency(self):
        return self._soundFile.samplerate

    # Processes audio file in blocks until all blocks are processed
    # Performs a given callback function on each channel after processing each block
    # Performs an additional callback function after processing all blocks if provided
    # param done_callback: an optional function that is called after processing all blocks of audio data
    # NOTE: will block for the length of the given audio file
    def processAudio(self, done_callback=None):
        block_duration = self._blockSize / self._soundFile.samplerate
        print(f"[audio] start: sr={self._soundFile.samplerate} Hz "
              f"channels={self._soundFile.channels} blockSize={self._blockSize} "
              f"block_duration={block_duration*1000:.2f}ms "
              f"cutoff={self._cutoffFreq}Hz order={self._filterOrder} sections={self._sos.shape[0]}")
        behind_count = 0
        start = time.monotonic()
        for i, block in enumerate(self._soundFile.blocks(blocksize=self._blockSize, always_2d=True)):
          block_t0 = time.monotonic()

          # If paused, block here until resumed and shift the timing baseline
          # forward so the deadline loop doesn't race to "catch up" on resume.
          if self.isPaused():
              pause_started = time.monotonic()
              print(f"[audio] block #{i}: paused")
              self._pauseEvent.wait()
              paused_for = time.monotonic() - pause_started
              start += paused_for
              print(f"[audio] block #{i}: resumed after {paused_for*1000:.1f}ms")

          # Downmix to mono before filtering.  The DAC's two channels carry
          # the low/high bands of one signal, so there is nowhere to send a
          # stereo pair.  always_2d gives (frames, channels); averaging to
          # (frames,) also prevents a stereo file from producing 2x the DAC
          # frames, which made playback run at half real-time.
          block = block.mean(axis=1)

          # Input block stats
          in_min = float(np.min(block))
          in_max = float(np.max(block))
          in_mean = float(np.mean(block))

          self._filterAudioBlock(block)

          hf_min = float(np.min(self._highFrequencyChannel))
          hf_max = float(np.max(self._highFrequencyChannel))
          lf_min = float(np.min(self._LowFrequencyChannel))
          lf_max = float(np.max(self._LowFrequencyChannel))

          cb_ms = 0.0
          if self._callback is not None:
              cb_t0 = time.monotonic()
              self._callback(self._highFrequencyChannel, self._LowFrequencyChannel)
              cb_ms = (time.monotonic() - cb_t0) * 1000.0

          deadline = start + (i + 1) * block_duration
          now = time.monotonic()
          delay = deadline - now
          work_ms = (now - block_t0) * 1000.0
          elapsed_ms = (now - start) * 1000.0
          drift_ms = (now - deadline) * 1000.0  # +ve means behind schedule

          print(
              f"[audio] block #{i}: shape={block.shape} dtype={block.dtype} "
              f"in[min={in_min:+.4f} max={in_max:+.4f} mean={in_mean:+.4f}] "
              f"hf[min={hf_min:+.4f} max={hf_max:+.4f}] lf[min={lf_min:+.4f} max={lf_max:+.4f}] "
              f"filter={self._lastFilterMs:.2f}ms cb={cb_ms:.2f}ms work={work_ms:.2f}ms "
              f"budget={block_duration*1000:.2f}ms "
              f"deadline_drift={drift_ms:+.2f}ms elapsed={elapsed_ms:.1f}ms"
          )

          if delay > 0:
              print(f"[audio] block #{i}: sleeping {delay*1000:.2f}ms to meet deadline")
              time.sleep(delay)
          else:
              behind_count += 1
              print(f"[audio] block #{i}: BEHIND by {-delay*1000:.2f}ms (total behind: {behind_count})")

        total_ms = (time.monotonic() - start) * 1000.0
        print(f"[audio] done: {i+1} blocks, {total_ms:.1f}ms elapsed, "
              f"{behind_count} blocks missed deadline")

        if done_callback is not None:
            done_callback()
