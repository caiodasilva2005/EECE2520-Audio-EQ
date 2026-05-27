
import socket
import struct

DEFAULT_SOCKET_PATH = '/run/audio-eq/audio.sock'
HEADER_FMT = '>cI'                       # 1-byte tag, 4-byte big-endian length
HANDSHAKE_FMT = '>I'                     # 4-byte big-endian uint32 sample rate (Hz)

# Class for managing client connection to the DAC Writer Daemon and sending audio data
class AudioClient():
    def __init__(self, audioProcesser):
        self._audioProcesser = audioProcesser
        self._connected = False

    def connectToDacWriterDaemon(self, socketPath=DEFAULT_SOCKET_PATH):
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.connect(socketPath)

        # Send handshake with sampling frequency
        self._socket.sendall(struct.pack(HANDSHAKE_FMT, self._audioProcesser.getSamplingFrequency()))
        # Set callback to send audio blocks to DAC writer daemon
        self._audioProcesser.setCallback(self._sendAudioBlockToDaemon)
        # Process audio file and send to DAC writer daemon

        self._connected = True

    def _sendAudioBlockToDaemon(self, highFrequencyChannel, lowFrequencyChannel):
        # Send low frequency channel with tag 'L'
        self._socket.sendall(struct.pack(HEADER_FMT, b'L', lowFrequencyChannel.nbytes))
        self._socket.sendall(lowFrequencyChannel.tobytes())
        # Send high frequency channel with tag 'H'
        self._socket.sendall(struct.pack(HEADER_FMT, b'H', highFrequencyChannel.nbytes))
        self._socket.sendall(highFrequencyChannel.tobytes())

    def processAudio(self, done_callback=None):
        if self._connected:
            self._audioProcesser.processAudio(done_callback)
        else:
            raise Exception("Cannot process audio. Client has not connected to the Daemon.")