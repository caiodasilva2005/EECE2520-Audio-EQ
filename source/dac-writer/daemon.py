import os
import socket
import struct

import numpy as np

DEFAULT_SOCKET_PATH = '/run/audio-eq/audio.sock'
HEADER_FMT = '>cI'                       # 1-byte tag, 4-byte big-endian length
HEADER_SIZE = struct.calcsize(HEADER_FMT)
HANDSHAKE_FMT = '>I'                     # 4-byte big-endian uint32 sample rate (Hz)
HANDSHAKE_SIZE = struct.calcsize(HANDSHAKE_FMT)
SAMPLE_DTYPE = np.float64

# Daemon Function to process samples for the DAC Writer
# param lowChannelCallback: Callback function to process low frequency samples
# param highChannelCallback: Callback function to process high frequency samples
# param onConnect: Callback function called upon connecting to a client
# param socketPath: Location of socket file in filesystem
def DACWriterDaemon(lowChannelCallback, highChannelCallback, onConnect=None,
                    socketPath=DEFAULT_SOCKET_PATH):

    def _recvExact(conn, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Peer Closed Connection")
            buf.extend(chunk)
        return bytes(buf)

    def _readHandshake(conn):
        (sampleRate,) = struct.unpack(HANDSHAKE_FMT, _recvExact(conn, HANDSHAKE_SIZE))
        return sampleRate

    def _readFrame(conn):
        tag, length = struct.unpack(HEADER_FMT, _recvExact(conn, HEADER_SIZE))
        samples = np.frombuffer(_recvExact(conn, length), dtype=SAMPLE_DTYPE)
        return tag, samples

    def _handleConnection(conn):
        try:
            # Receive sampling frequency from handshake
            sampleRate = _readHandshake(conn)
        except ConnectionError:
            return
        print(f"Client sample rate: {sampleRate} Hz")
        if onConnect is not None:
            # Callback upon receiving sample frequency
            onConnect(sampleRate)
        while True:
            try:
                # Daemon expects samples as tag ('L' or 'H') and array of bytes for the audio sample
                tag, samples = _readFrame(conn)
            except ConnectionError:
                return
            if tag == b'L':
                # Callback to process low frequency channel
                lowChannelCallback(samples)
            elif tag == b'H':
                # Callback to process high frequency channel
                highChannelCallback(samples)
            else:
                print(f"unknown tag: {tag!r}")

    # Runs the main daemon process
    def startDaemon():
        if os.path.exists(socketPath):
            os.unlink(socketPath)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.bind(socketPath)
            s.listen()
            print(f"DAC writer daemon listening on {socketPath}")
            while True:
                conn, _ = s.accept()
                with conn:
                    print("Client Connected")
                    _handleConnection(conn)
                    print("Client Disconnected")

    return startDaemon

