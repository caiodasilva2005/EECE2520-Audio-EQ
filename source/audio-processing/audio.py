
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
        self._highFrequencyChannel, self._zi = sig.sosfilt(self._sos, audioBlock, zi=self._zi)
        self._LowFrequencyChannel = audioBlock - self._highFrequencyChannel

    # sets the filter order and rebuilds the second-order sections for the Butterworth high-pass filter
    def setFilterOrder(self, filterOrder):
        self._filterOrder = filterOrder
        self._sos = self._buildSos()
    
    # sets the cutoff frequency and rebuilds the second-order sections for the Butterworth high-pass filter
    def setCutoffFrequency(self, cutoffFreq):
        self._cutoffFreq = cutoffFreq
        self._sos = self._buildSos()

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
        start = time.monotonic()
        for i, block in enumerate(self._soundFile.blocks(blocksize=self._blockSize)):
          # If paused, block here until resumed and shift the timing baseline
          # forward so the deadline loop doesn't race to "catch up" on resume.
          if self.isPaused():
              pause_started = time.monotonic()
              self._pauseEvent.wait()
              start += time.monotonic() - pause_started

          self._filterAudioBlock(block)

          if self._callback is not None:
              self._callback(self._highFrequencyChannel, self._LowFrequencyChannel)

          deadline = start + (i + 1) * block_duration
          delay = deadline - time.monotonic()
          if delay > 0:
              time.sleep(delay)
    
        if done_callback is not None:
            done_callback()
