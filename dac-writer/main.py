
import numpy as np
from daemon import DACWriterDaemon
import time

TWELVE_BIT_MAX_RESOLUTION = 0x0FFF

DAC1 = "/sys/bus/iio/devices/iio:device0/out_voltage0_raw"
DAC2 = "/sys/bus/iio/devices/iio:device0/out_voltage1_raw"

# Class for managing Writer to an Adafruit AD569R DAC
class DAC:
    def __init__(self, dac):
        try:
            self._dac = open(dac, 'wb', buffering=0)
        except:
            raise Exception('Failed to open path to DAC driver. Is the kernel module loaded?')
        self._samplingFrequnecy = 0

    def setSamplingFrequency(self, samplingFrequncy):
        self._samplingFrequnecy = samplingFrequncy

    def writeSampleToDAC(self, sample):
        # shift and scale the incoming sample to a value within 16-bit resolution
        codes = np.clip((sample + 1.0) * (TWELVE_BIT_MAX_RESOLUTION / 2), 0, TWELVE_BIT_MAX_RESOLUTION).astype(np.uint16)
        
        # write to daq with slight delay based on sampling frequency to reflect speed of audio
        start = time.monotonic()
        for i, code in enumerate(codes):
          self._dac.seek(0)
          self._dac.write(str(int(code)).encode())
          deadline = start + (i + 1) * (1.0 / self._samplingFrequnecy)
          delay = deadline - time.monotonic()
          if delay > 0:
              time.sleep(delay)

def main():

    dac1 = DAC(DAC1)
    dac2 = DAC(DAC2)

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