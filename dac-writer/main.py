
from adafruit_extended_bus import ExtendedI2C
import adafruit_ad569x
import numpy as np
from daemon import DACWriterDaemon
import time

SIXTEEN_BIT_MAX_RESOLUTION = 0xFFFF

DAC1_I2C_BUS = 1 # /dev/i2c-1
DAC2_I2C_BUS = 3 # /dev/i2c-3

# I2C buses on Raspberry Pi 4B
i2c1 = ExtendedI2C(DAC1_I2C_BUS)
i2c2 = ExtendedI2C(DAC2_I2C_BUS)

# Initialize the DACs
dacDevice1 = adafruit_ad569x.Adafruit_AD569x(i2c1) # used for high frequency channel
dacDevice2 = adafruit_ad569x.Adafruit_AD569x(i2c2) # used for low frequency channel

# Class for managing Writer to an Adafruit AD569R DAC
class DAC:
    def __init__(self, dacDevice):
        self._dacDevice = dacDevice
        self._samplingFrequnecy = 0

    def setSamplingFrequency(self, samplingFrequncy):
        self._samplingFrequnecy = samplingFrequncy

    def writeSampleToDAC(self, sample):
        # shift and scale the incoming sample to a value within 16-bit resolution
        codes = np.clip((sample + 1.0) * (SIXTEEN_BIT_MAX_RESOLUTION / 2), 0, SIXTEEN_BIT_MAX_RESOLUTION).astype(np.uint16)
        
        # write to daq with slight delay based on sampling frequency to reflect speed of audio
        start = time.monotonic()
        for i, code in enumerate(codes):
          self._dacDevice.value = int(code)
          deadline = start + (i + 1) * (1.0 / self._samplingFrequnecy)
          delay = deadline - time.monotonic()
          if delay > 0:
              time.sleep(delay)

def main():

    dac1 = DAC(dacDevice1)
    dac2 = DAC(dacDevice2)

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