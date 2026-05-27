
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
        
        # shift and scale the incoming sample to a value within 16-bit resolution
        scaled = (sample + 1.0) * (TWELVE_BIT_MAX_RESOLUTION / 2)
        codes = np.clip(scaled, 0, TWELVE_BIT_MAX_RESOLUTION).astype(np.uint16)

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