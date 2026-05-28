import numpy as np
from daemon import DACWriterDaemon
import time
import os

TWELVE_BIT_MAX_RESOLUTION = 0x0FFF

# IIO sysfs and device paths
IIO_SYSFS          = "/sys/bus/iio/devices/iio:device0"
IIO_DEV            = "/dev/iio:device0"
TRIGGER_NAME       = "mcp4822-dev0"

# Buffer depth in samples — must be power of two.
# Larger = fewer write() calls but more latency.
BUFFER_LEN         = 4096


def iio_sysfs_write(relative_path, value):
    path = f"{IIO_SYSFS}/{relative_path}"
    with open(path, 'w') as f:
        f.write(str(value))


def configure_iio_buffer(sample_rate_hz):
    """
    Configure the IIO triggered buffer once at startup.
    This replaces the per-sample sysfs writes and time.sleep() pacing.
    The hrtimer in the kernel fires at sample_rate_hz and drains
    one sample per tick — no userspace timing needed at all.
    """
    # Set the sample rate — kernel hrtimer handles all pacing from here
    iio_sysfs_write("sample_rate_hz", int(sample_rate_hz))

    # Enable both DAC channels in the scan
    iio_sysfs_write("scan_elements/out_voltage0_en", 1)
    iio_sysfs_write("scan_elements/out_voltage1_en", 1)

    # Set kfifo depth and link our hrtimer trigger
    iio_sysfs_write("buffer/length", BUFFER_LEN)
    iio_sysfs_write("trigger/current_trigger", TRIGGER_NAME)

    # Enable the buffer — this starts the hrtimer in the kernel
    iio_sysfs_write("buffer/enable", 1)

    print(f"[IIO] buffer configured: rate={sample_rate_hz}Hz "
          f"trigger={TRIGGER_NAME} buffer_len={BUFFER_LEN}")


def teardown_iio_buffer():
    """Stop the hrtimer and flush the kfifo on exit."""
    try:
        iio_sysfs_write("buffer/enable", 0)
        print("[IIO] buffer disabled")
    except Exception as e:
        print(f"[IIO] teardown warning: {e}")

# Scale float [-1, 1] → 12-bit unsigned [0, 4095]
def Scale(s):
    scaled = (s + 1.0) * (TWELVE_BIT_MAX_RESOLUTION / 2.0)
    return np.clip(scaled, 0, TWELVE_BIT_MAX_RESOLUTION).astype(np.uint16)

class DAC:
    def __init__(self, name="DAC"):
        """
        A single DAC instance now represents both channels together,
        since the IIO buffer interleaves ch0 and ch1 in the same write().
        """
        try:
            # O_WRONLY | O_NONBLOCK so write() returns EAGAIN instead of
            # blocking forever if the kfifo is completely full
            fd = os.open(IIO_DEV, os.O_WRONLY | os.O_NONBLOCK)
            self._dev = os.fdopen(fd, 'wb', buffering=0)
        except Exception as e:
            raise Exception(
                f"Failed to open IIO device {IIO_DEV}: {e}\n"
                "Is the kernel module loaded and buffer enabled?"
            )
        self._name = name
        self._block_count = 0
        print(f"[{self._name}] opened {IIO_DEV}")

    def writeSamplesToDAC(self, samples_ch0, samples_ch1):
        """
        Write a full block of samples for both channels in one syscall.

        samples_ch0, samples_ch1: numpy arrays of float in [-1.0, 1.0]
        representing channel A (low band) and channel B (high band).

        The IIO buffer frame layout for MCP4822 with both channels active is:
            [ uint16 ch0 | uint16 ch1 ]  per sample
        packed little-endian, which is what struct.pack('<H') gives.

        The kernel hrtimer pops one frame per tick at sample_rate_hz —
        no sleep() or deadline arithmetic needed here at all.
        """
        self._block_count += 1
        block_idx = self._block_count

        assert len(samples_ch0) == len(samples_ch1), \
            "Channel sample arrays must be the same length"

        n = len(samples_ch0)
        codes_ch0 = Scale(samples_ch0)
        codes_ch1 = Scale(samples_ch1)

        # Pack interleaved frames: [ch0_0, ch1_0, ch0_1, ch1_1, ...]
        # Each uint16 is 2 bytes little-endian — matches IIO buffer layout
        interleaved = np.empty(n * 2, dtype=np.uint16)
        interleaved[0::2] = codes_ch0
        interleaved[1::2] = codes_ch1
        raw_bytes = interleaved.tobytes()

        # Write the entire block in one syscall.
        # The kernel kfifo absorbs all frames immediately.
        # write() may return short if the kfifo is nearly full,
        # so loop until all bytes are written.
        view = memoryview(raw_bytes)
        total = len(raw_bytes)
        written = 0
        while written < total:
            try:
                n_written = self._dev.write(view[written:])
                written += n_written
            except BlockingIOError:
                # kfifo temporarily full — yield briefly and retry
                # This should be rare with a well-sized BUFFER_LEN
                time.sleep(0.001)

        print(f"[{self._name}] block #{block_idx}: "
              f"wrote {n} frames ({total} bytes) in one syscall")

    def close(self):
        self._dev.close()


def main():
    dac = DAC(name="DAC-AB")

    def set_sampling_frequency(sample_rate_hz):
        print("Set sampling frequency:", sample_rate_hz)
        configure_iio_buffer(sample_rate_hz)

    def write_samples(samples_ch0, samples_ch1):
        print("WRITING SAMPLES")
        dac.writeSamplesToDAC(samples_ch0, samples_ch1)

    daemon_process = DACWriterDaemon(
        write_samples,
        set_sampling_frequency,
    )

    try:
        daemon_process()
    finally:
        teardown_iio_buffer()
        dac.close()


if __name__ == "__main__":
    main()