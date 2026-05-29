// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Copyright (C) 2023 Anshul Dalal <anshulusr@gmail.com>
 *
 * Driver for Microchip MCP4801, MCP4802, MCP4811, MCP4812, MCP4821 and MCP4822
 *
 * Based on the work of:
 *   Michael Welling (MCP4922 Driver)
 *
 * Datasheet:
 *   MCP48x1: https://ww1.microchip.com/downloads/en/DeviceDoc/22244B.pdf
 *   MCP48x2: https://ww1.microchip.com/downloads/en/DeviceDoc/20002249B.pdf
 *
 * Buffered output + hrtimer trigger added for audio sample playback.
 *
 * TODO:
 *   - Configurable gain
 *   - Regulator control
 */

#include <linux/module.h>
#include <linux/mod_devicetable.h>
#include <linux/spi/spi.h>
#include <linux/iio/iio.h>
#include <linux/iio/types.h>
#include <linux/iio/buffer.h>
#include <linux/iio/trigger.h>
#include <linux/iio/trigger_consumer.h>
#include <linux/iio/triggered_buffer.h>
#include <linux/unaligned.h>
#include <linux/hrtimer.h>
#include <linux/ktime.h>

#define MCP4821_ACTIVE_MODE     BIT(12)
#define MCP4802_SECOND_CHAN      BIT(15)

/* DAC uses an internal Voltage reference of 4.096V at a gain of 2x */
#define MCP4821_2X_GAIN_VREF_MV 4096

/* Default sample rate in Hz used by the hrtimer trigger */
#define MCP4821_DEFAULT_SAMPLE_RATE_HZ  44100

enum mcp4821_supported_drvice_ids {
	ID_MCP4801,
	ID_MCP4802,
	ID_MCP4811,
	ID_MCP4812,
	ID_MCP4821,
	ID_MCP4822,
};

struct mcp4821_state {
	struct spi_device       *spi;
	u16                      dac_value[2];

	/* --- hrtimer trigger members --- */
	struct iio_trigger      *trig;
	struct hrtimer           timer;
	unsigned int             sample_rate_hz;   /* configurable via sysfs */
};

struct mcp4821_chip_info {
	const char              *name;
	int                      num_channels;
	const struct iio_chan_spec channels[2];
};

/*
 * SCAN_INDEX must be set so the IIO buffer knows which channel slot a sample
 * belongs to.  We also set BIT(IIO_CHAN_INFO_RAW) in scan_type so the core
 * knows the raw value lives in the buffer word.
 */
#define MCP4821_CHAN(channel_id, resolution)				\
{									\
	.type            = IIO_VOLTAGE,					\
	.output          = 1,						\
	.indexed         = 1,						\
	.channel         = (channel_id),				\
	.scan_index      = (channel_id),				\
	.info_mask_separate      = BIT(IIO_CHAN_INFO_RAW),		\
	.info_mask_shared_by_type = BIT(IIO_CHAN_INFO_SCALE),		\
	.scan_type = {							\
		.sign        = 'u',					\
		.realbits    = (resolution),				\
		.storagebits = 16,					\
		.shift       = 12 - (resolution),			\
		.endianness  = IIO_CPU,					\
	},								\
}

static const struct mcp4821_chip_info mcp4821_chip_info_table[6] = {
	[ID_MCP4801] = {
		.name         = "mcp4801",
		.num_channels = 1,
		.channels     = { MCP4821_CHAN(0, 8) },
	},
	[ID_MCP4802] = {
		.name         = "mcp4802",
		.num_channels = 2,
		.channels     = { MCP4821_CHAN(0, 8), MCP4821_CHAN(1, 8) },
	},
	[ID_MCP4811] = {
		.name         = "mcp4811",
		.num_channels = 1,
		.channels     = { MCP4821_CHAN(0, 10) },
	},
	[ID_MCP4812] = {
		.name         = "mcp4812",
		.num_channels = 2,
		.channels     = { MCP4821_CHAN(0, 10), MCP4821_CHAN(1, 10) },
	},
	[ID_MCP4821] = {
		.name         = "mcp4821",
		.num_channels = 1,
		.channels     = { MCP4821_CHAN(0, 12) },
	},
	[ID_MCP4822] = {
		.name         = "mcp4822",
		.num_channels = 2,
		.channels     = { MCP4821_CHAN(0, 12), MCP4821_CHAN(1, 12) },
	},
};

/* --------------------------------------------------------------------------
 * Low-level SPI helper (used by both direct write and trigger handler)
 * -------------------------------------------------------------------------- */

static int mcp4821_spi_write(struct mcp4821_state *state,
			     int channel, u16 raw_val,
			     unsigned int shift)
{
	u16 write_val;
	__be16 write_buffer;

	write_val = MCP4821_ACTIVE_MODE | (raw_val << shift);
	if (channel)
		write_val |= MCP4802_SECOND_CHAN;

	write_buffer = cpu_to_be16(write_val);
	return spi_write(state->spi, &write_buffer, sizeof(write_buffer));
}

/* --------------------------------------------------------------------------
 * IIO info callbacks (direct / sysfs access — unchanged behaviour)
 * -------------------------------------------------------------------------- */

static int mcp4821_read_raw(struct iio_dev *indio_dev,
			    struct iio_chan_spec const *chan, int *val,
			    int *val2, long mask)
{
	struct mcp4821_state *state;

	switch (mask) {
	case IIO_CHAN_INFO_RAW:
		state = iio_priv(indio_dev);
		*val  = state->dac_value[chan->channel];
		return IIO_VAL_INT;
	case IIO_CHAN_INFO_SCALE:
		*val  = MCP4821_2X_GAIN_VREF_MV;
		*val2 = chan->scan_type.realbits;
		return IIO_VAL_FRACTIONAL_LOG2;
	default:
		return -EINVAL;
	}
}

static int mcp4821_write_raw(struct iio_dev *indio_dev,
			     struct iio_chan_spec const *chan, int val,
			     int val2, long mask)
{
	struct mcp4821_state *state = iio_priv(indio_dev);
	int ret;

	if (val2 != 0)
		return -EINVAL;
	if (val < 0 || val >= BIT(chan->scan_type.realbits))
		return -EINVAL;
	if (mask != IIO_CHAN_INFO_RAW)
		return -EINVAL;

	ret = iio_device_claim_direct(indio_dev);
   	if (ret) {
	        return ret;
	}

	ret = mcp4821_spi_write(state, chan->channel, val,
				chan->scan_type.shift);
	if (ret) {
		dev_err(&state->spi->dev, "Failed to write to device: %d", ret);
		return ret;
	}

	state->dac_value[chan->channel] = val;
	return 0;
}

static const struct iio_info mcp4821_info = {
	.read_raw  = &mcp4821_read_raw,
	.write_raw = &mcp4821_write_raw,
};

/* --------------------------------------------------------------------------
 * IIO triggered-buffer handler
 *
 * Called in a kernel thread each time the hrtimer trigger fires.
 * It pops one sample word per active channel from the IIO kfifo, then
 * sends it to the DAC over SPI.
 * -------------------------------------------------------------------------- */

static irqreturn_t mcp4821_trigger_handler(int irq, void *p)
{
	struct iio_poll_func   *pf        = p;
	struct iio_dev         *indio_dev = pf->indio_dev;
	struct mcp4821_state   *state     = iio_priv(indio_dev);

	/*
	 * Buffer layout: one u16 per active channel, in scan_index order.
	 * Max 2 channels → 2 × u16 = 4 bytes.  No timestamp needed for DAC
	 * output, but allocate space for one anyway for alignment.
	 */
	u16 data[2];
	int ret;
	int i = 0;
	int ch;

	/*
	 * iio_pop_from_buffer() atomically removes one complete sample set
	 * (all active channels) that user space previously wrote into the
	 * /dev/iio:deviceX buffer via write().
	 */
	ret = iio_pop_from_buffer(indio_dev->buffer, data);
	if (ret)
		goto done;   /* buffer underrun — keep trigger alive, skip beat */

	for_each_set_bit(ch, indio_dev->active_scan_mask,
			 indio_dev->num_channels) {
		ret = mcp4821_spi_write(state, ch, data[i],
					indio_dev->channels[ch].scan_type.shift);
		if (ret)
			dev_err_ratelimited(&state->spi->dev,
					    "SPI write ch%d failed: %d\n",
					    ch, ret);
		i++;
	}

done:
	iio_trigger_notify_done(indio_dev->trig);
	return IRQ_HANDLED;
}

/* --------------------------------------------------------------------------
 * hrtimer-based IIO trigger
 *
 * The kernel's IIO trigger subsystem lets user space associate any trigger
 * source with a buffer.  We create a private software trigger backed by an
 * hrtimer running at sample_rate_hz.  Each timer expiry calls
 * iio_trigger_poll(), which schedules the trigger handler thread above.
 * -------------------------------------------------------------------------- */

static enum hrtimer_restart mcp4821_hrtimer_cb(struct hrtimer *timer)
{
	struct mcp4821_state *state =
		container_of(timer, struct mcp4821_state, timer);

	iio_trigger_poll(state->trig);

	/* Re-arm for the next sample period */
	hrtimer_forward_now(timer,
			    ns_to_ktime(NSEC_PER_SEC / state->sample_rate_hz));
	return HRTIMER_RESTART;
}

/* Trigger enable / disable callbacks */
static int mcp4821_trig_set_state(struct iio_trigger *trig, bool state_on)
{
	struct iio_dev       *indio_dev = iio_trigger_get_drvdata(trig);
	struct mcp4821_state *state     = iio_priv(indio_dev);

	if (state_on) {
		hrtimer_start(&state->timer,
			      ns_to_ktime(NSEC_PER_SEC / state->sample_rate_hz),
			      HRTIMER_MODE_REL);
	} else {
		hrtimer_cancel(&state->timer);
	}
	return 0;
}

static const struct iio_trigger_ops mcp4821_trigger_ops = {
	.set_trigger_state = mcp4821_trig_set_state,
};

/* --------------------------------------------------------------------------
 * sysfs: expose sample_rate_hz so user space can change it at runtime
 * -------------------------------------------------------------------------- */

static ssize_t sample_rate_hz_show(struct device *dev,
				   struct device_attribute *attr, char *buf)
{
	struct iio_dev       *indio_dev = dev_to_iio_dev(dev);
	struct mcp4821_state *state     = iio_priv(indio_dev);

	return sysfs_emit(buf, "%u\n", state->sample_rate_hz);
}

static ssize_t sample_rate_hz_store(struct device *dev,
				    struct device_attribute *attr,
				    const char *buf, size_t count)
{
	struct iio_dev       *indio_dev = dev_to_iio_dev(dev);
	struct mcp4821_state *state     = iio_priv(indio_dev);
	unsigned int          rate;
	int ret;

	ret = kstrtouint(buf, 10, &rate);
	if (ret)
		return ret;
	if (rate == 0 || rate > 192000)
		return -EINVAL;

	state->sample_rate_hz = rate;
	return count;
}

static DEVICE_ATTR_RW(sample_rate_hz);

static struct attribute *mcp4821_attrs[] = {
        &dev_attr_sample_rate_hz.attr,
        NULL,
};

static const struct attribute_group mcp4821_attr_group = {
	.attrs = mcp4821_attrs,
};

/* Extend iio_info to include the attribute group */
static const struct iio_info mcp4821_info_with_buffer = {
	.read_raw       = &mcp4821_read_raw,
	.write_raw      = &mcp4821_write_raw,
	.attrs          = &mcp4821_attr_group,
};

/* --------------------------------------------------------------------------
 * probe / remove
 * -------------------------------------------------------------------------- */

static int mcp4821_probe(struct spi_device *spi)
{
	struct iio_dev             *indio_dev;
	struct mcp4821_state       *state;
	const struct mcp4821_chip_info *info;
	int ret;

	indio_dev = devm_iio_device_alloc(&spi->dev, sizeof(*state));
	if (!indio_dev)
		return -ENOMEM;

	state = iio_priv(indio_dev);
	state->spi            = spi;
	state->sample_rate_hz = MCP4821_DEFAULT_SAMPLE_RATE_HZ;

	info = spi_get_device_match_data(spi);

	indio_dev->name        = info->name;
	indio_dev->info        = &mcp4821_info_with_buffer;
	/* INDIO_BUFFER_SOFTWARE enables the IIO kfifo buffer that user space
	 * writes into; INDIO_DIRECT_MODE keeps sysfs raw writes working. */
	indio_dev->modes       = INDIO_DIRECT_MODE | INDIO_BUFFER_SOFTWARE;
	indio_dev->channels    = info->channels;
	indio_dev->num_channels = info->num_channels;

	/* --- hrtimer trigger --- */
	state->trig = devm_iio_trigger_alloc(&spi->dev, "%s-dev%d",
					     indio_dev->name,
					     iio_device_id(indio_dev));
	if (!state->trig)
		return -ENOMEM;

	state->trig->ops = &mcp4821_trigger_ops;
	iio_trigger_set_drvdata(state->trig, indio_dev);

	ret = devm_iio_trigger_register(&spi->dev, state->trig);
	if (ret)
		return ret;

	/* Associate the trigger with the device by default */
	indio_dev->trig = iio_trigger_get(state->trig);

	hrtimer_setup(&state->timer, mcp4821_hrtimer_cb, CLOCK_MONOTONIC, HRTIMER_MODE_REL);	
	state->timer.function = mcp4821_hrtimer_cb;

	/* --- IIO triggered buffer setup ---
	 * This is a DAC: user space *writes* samples into the buffer, so it
	 * MUST be created with IIO_BUFFER_DIRECTION_OUT.  The plain
	 * devm_iio_triggered_buffer_setup() hardcodes DIRECTION_IN, which makes
	 * the IIO core reject write() with -EPERM. */
	ret = devm_iio_triggered_buffer_setup_ext(&spi->dev, indio_dev,
						  &iio_pollfunc_store_time,
						  &mcp4821_trigger_handler,
						  IIO_BUFFER_DIRECTION_OUT,
						  NULL,
						  NULL);
	if (ret)
		return ret;

	return devm_iio_device_register(&spi->dev, indio_dev);
}

/* --------------------------------------------------------------------------
 * Module boilerplate (unchanged from original)
 * -------------------------------------------------------------------------- */

#define MCP4821_COMPATIBLE(of_compatible, id)	\
{						\
	.compatible = of_compatible,		\
	.data = &mcp4821_chip_info_table[id]	\
}

static const struct of_device_id mcp4821_of_table[] = {
	MCP4821_COMPATIBLE("microchip,mcp4801", ID_MCP4801),
	MCP4821_COMPATIBLE("microchip,mcp4802", ID_MCP4802),
	MCP4821_COMPATIBLE("microchip,mcp4811", ID_MCP4811),
	MCP4821_COMPATIBLE("microchip,mcp4812", ID_MCP4812),
	MCP4821_COMPATIBLE("microchip,mcp4821", ID_MCP4821),
	MCP4821_COMPATIBLE("microchip,mcp4822", ID_MCP4822),
	{ }
};
MODULE_DEVICE_TABLE(of, mcp4821_of_table);

static const struct spi_device_id mcp4821_id_table[] = {
	{ "mcp4801", (kernel_ulong_t)&mcp4821_chip_info_table[ID_MCP4801] },
	{ "mcp4802", (kernel_ulong_t)&mcp4821_chip_info_table[ID_MCP4802] },
	{ "mcp4811", (kernel_ulong_t)&mcp4821_chip_info_table[ID_MCP4811] },
	{ "mcp4812", (kernel_ulong_t)&mcp4821_chip_info_table[ID_MCP4812] },
	{ "mcp4821", (kernel_ulong_t)&mcp4821_chip_info_table[ID_MCP4821] },
	{ "mcp4822", (kernel_ulong_t)&mcp4821_chip_info_table[ID_MCP4822] },
	{ }
};
MODULE_DEVICE_TABLE(spi, mcp4821_id_table);

static struct spi_driver mcp4821_driver = {
	.driver = {
		.name           = "mcp4821",
		.of_match_table = mcp4821_of_table,
	},
	.probe    = mcp4821_probe,
	.id_table = mcp4821_id_table,
};
module_spi_driver(mcp4821_driver);

MODULE_AUTHOR("Anshul Dalal <anshulusr@gmail.com>");
MODULE_DESCRIPTION("Microchip MCP4821 DAC Driver (with triggered buffer output)");
MODULE_LICENSE("GPL");
