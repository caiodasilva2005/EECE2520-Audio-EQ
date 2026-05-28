import os
import queue
import socket
import struct
import threading

import numpy as np

DEFAULT_SOCKET_PATH = '/run/audio-eq/audio.sock'
HEADER_FMT = '>cI'                       # 1-byte tag, 4-byte big-endian length
HEADER_SIZE = struct.calcsize(HEADER_FMT)
HANDSHAKE_FMT = '>I'                     # 4-byte big-endian uint32 sample rate (Hz)
HANDSHAKE_SIZE = struct.calcsize(HANDSHAKE_FMT)
SAMPLE_DTYPE = np.float64

# Daemon Function to process samples for the DAC Writer
# param channelCallback: Callback function to process samples
# param onConnect: Callback function called upon connecting to a client
# param socketPath: Location of socket file in filesystem
def DACWriterDaemon(channelCallback, onConnect=None,
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

    # Drains a per-channel queue until a sentinel (None) is received,
    # invoking the given callback for each sample buffer.
    def _writerLoop(q, callback, name):
        while True:
            samples = q.get()
            if samples is None:
                return
            try:
                callback(samples)
            except Exception as e:
                print(f"[{name}] callback error: {e}")

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

        # Bounded queues keep backpressure tight: if a writer stalls, the
        # reader blocks on put, which blocks the socket read, which surfaces
        # the stall to the audio producer instead of hiding it in a deep
        # kernel/userspace buffer.
        frameQueue = queue.Queue(maxsize=4)

        def _writerLoop():
            while True:
                item = frameQueue.get()
                if item is None:
                    return
                low_samples, high_samples = item
                try:
                    channelCallback(low_samples, high_samples)
                except Exception as e:
                    print(f"[DAC] callback error: {e}")
        
        writer = threading.Thread(
        target=_writerLoop, name="dac-writer", daemon=True)
        writer.start()

        pending = {}
        try:
            while True:
                try:
                    # Daemon expects samples as tag ('L' or 'H') and array of bytes for the audio sample
                    tag, samples = _readFrame(conn)
                except ConnectionError:
                    return
                if tag in (b'L', b'H'):
                    pending[tag] = samples
                    if b'L' in pending and b'H' in pending:
                        frameQueue.put((pending.pop(b'L'), pending.pop(b'H')))
                else:
                    print(f"unknown tag: {tag!r}")
        finally:
            # Drain remaining work and shut writers down cleanly.
            frameQueue.put(None)
            writer.join()

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

