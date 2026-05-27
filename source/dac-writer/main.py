
import numpy as np
from daemon import DACWriterDaemon
import time

TWELVE_BIT_MAX_RESOLUTION = 0x0FFF

DAC1 = "/sys/bus/iio/devices/iio:device0/out_voltage0_raw" # DAC Channel A
DAC2 = "/sys/bus/iio/devices/iio:device0/out_voltage1_raw" # DAC Channel B

# Class for managing Writer to an Microchip MCP4822 DAC
class DAC:
    def __init__(self, dac, name="DAC"):
        try:
            self._dac = open(dac, 'wb', buffering=0)
        except:
            raise Exception('Failed to open path to DAC driver. Is the kernel module loaded?')
        self._samplingFrequnecy = 0
        self._name = name
        self._blockCount = 0
        print(f"[{self._name}] opened path: {dac}")

    def setSamplingFrequency(self, samplingFrequncy):
        self._samplingFrequnecy = samplingFrequncy
        print(f"[{self._name}] sampling frequency set to {samplingFrequncy} Hz")

    def writeSampleToDAC(self, sample):
        self._blockCount += 1
        block_idx = self._blockCount

        # incoming sample stats
        n = len(sample)
        sample_min = float(np.min(sample)) if n else float('nan')
        sample_max = float(np.max(sample)) if n else float('nan')
        sample_mean = float(np.mean(sample)) if n else float('nan')
        sample_rms = float(np.sqrt(np.mean(np.square(sample, dtype=np.float64)))) if n else float('nan')
        head_preview = np.array2string(sample[:8], precision=4, separator=", ")
        out_of_range = int(np.sum((sample < -1.0) | (sample > 1.0)))
        print(
            f"[{self._name}] block #{block_idx}: n={n} dtype={sample.dtype} "
            f"min={sample_min:+.4f} max={sample_max:+.4f} mean={sample_mean:+.4f} rms={sample_rms:.4f} "
            f"out_of_[-1,1]={out_of_range} head={head_preview}"
        )

        # shift and scale the incoming sample to a value within 16-bit resolution
        scaled = (sample + 1.0) * (TWELVE_BIT_MAX_RESOLUTION / 2)
        clipped_count = int(np.sum((scaled < 0) | (scaled > TWELVE_BIT_MAX_RESOLUTION)))
        codes = np.clip(scaled, 0, TWELVE_BIT_MAX_RESOLUTION).astype(np.uint16)

        # outgoing code stats
        code_min = int(np.min(codes)) if n else -1
        code_max = int(np.max(codes)) if n else -1
        code_mean = float(np.mean(codes)) if n else float('nan')
        code_head = np.array2string(codes[:8], separator=", ")
        print(
            f"[{self._name}] block #{block_idx} codes: min={code_min} max={code_max} mean={code_mean:.1f} "
            f"clipped={clipped_count}/{n} head={code_head}"
        )

        if self._samplingFrequnecy <= 0:
            print(f"[{self._name}] WARNING: sampling frequency not set; pacing disabled")

        # write to daq with slight delay based on sampling frequency to reflect speed of audio
        start = time.monotonic()
        for i, code in enumerate(codes):
          self._dac.seek(0)
          self._dac.write(str(int(code)).encode())
          deadline = start + (i + 1) * (1.0 / self._samplingFrequnecy)
          delay = deadline - time.monotonic()
          if delay > 0:
              time.sleep(delay)

        elapsed = time.monotonic() - start
        expected = n / self._samplingFrequnecy if self._samplingFrequnecy else float('nan')
        print(
            f"[{self._name}] block #{block_idx} write done: elapsed={elapsed*1000:.2f}ms "
            f"expected={expected*1000:.2f}ms drift={(elapsed-expected)*1000:+.2f}ms"
        )

def main():

    dac1 = DAC(DAC1, name="DAC-A/low")
    dac2 = DAC(DAC2, name="DAC-B/high")

    daemon_process = DACWriterDaemon(
        dac1.writeSampleToDAC,
        dac2.writeSampleToDAC,
        lambda samplingFrequency: (
            dac1.setSamplingFrequency(samplingFrequency),
            dac2.setSamplingFrequency(samplingFrequency),
        ),
    )
    
    # Start Daemon
    daemon_process()

if __name__ == "__main__":
    main()