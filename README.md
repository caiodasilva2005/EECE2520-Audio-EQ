# EECE2520 Audio EQ

This project streams audio through a split-band pipeline:

- Audio processing client reads a file, splits it into low/high bands, and sends frames.
- DAC writer daemon receives paired frames and writes interleaved samples to the IIO DAC device.

## Logic Branch

Use this branch logic for the runtime data path:

```text
START
	|
	v
Client connects to DAC daemon?
	|- No  -> Return error: "Socket unavailable" and stop.
	|- Yes -> Send handshake (sample rate).
						|
						v
			Read next audio block available?
				|- No  -> End stream, close socket.
				|- Yes -> Split into LOW + HIGH bands.
									|
									v
						Daemon has both tags (L and H)?
							|- No  -> Keep buffering pending frame.
							|- Yes -> Enqueue paired frame.
												|
												v
									IIO buffer ready?
										|- No  -> Drop frame and log warning.
										|- Yes -> Scale, interleave, write to /dev/iio:device0.
```

## Notes

- The daemon uses bounded queue backpressure to avoid unbounded memory growth.
- Kernel-side IIO timing controls output sample pacing after buffer setup.

