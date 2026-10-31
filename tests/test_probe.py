import unittest
from types import SimpleNamespace

from recall import probe

DUMP = """HW Params of device "hw:XR18,0":
--------------------
ACCESS:  MMAP_INTERLEAVED RW_INTERLEAVED
FORMAT:  S24_3LE
SAMPLE_BITS: 24
FRAME_BITS: [48 432]
CHANNELS: [2 18]
RATE: [44100 48000]
PERIOD_TIME: (83 11888617]
--------------------
"""
# at 18 channels the device only offers 48 kHz; at 2 it offers more
AT_18 = DUMP.replace("CHANNELS: [2 18]", "CHANNELS: 18").replace("[44100 48000]", "48000")
AT_2 = DUMP.replace("CHANNELS: [2 18]", "CHANNELS: 2").replace("[44100 48000]", "[8000 96000]")


def fake(by_channels):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        n = int(cmd[cmd.index("-c") + 1]) if "-c" in cmd else None
        return SimpleNamespace(stdout="", stderr=by_channels[n])

    run.calls = calls
    return run


class Parse(unittest.TestCase):
    def test_ranges_and_single_values(self):
        self.assertEqual(probe.parse_ranges(DUMP), {"CHANNELS": (2, 18), "RATE": (44100, 48000)})
        self.assertEqual(probe.parse_ranges(AT_18), {"CHANNELS": (18, 18), "RATE": (48000, 48000)})

    def test_missing_fields(self):
        with self.assertRaises(probe.ProbeError):
            probe.parse_ranges("nothing useful")


class Select(unittest.TestCase):
    def test_max_channels_then_rate_at_that_count(self):
        run = fake({None: DUMP, 18: AT_18, 2: AT_2})
        self.assertEqual(probe.select("hw:CARD=XR18,DEV=0", run=run), probe.Format(18, 48000))
        self.assertIn("-D", run.calls[0])
        self.assertIn("hw:CARD=XR18,DEV=0", run.calls[0])

    def test_channel_ceiling_changes_rate_lookup(self):
        run = fake({None: DUMP, 18: AT_18, 2: AT_2})
        self.assertEqual(probe.select("hw", max_channels=2, run=run), probe.Format(2, 96000))

    def test_rate_ceiling(self):
        run = fake({None: DUMP, 2: AT_2})
        self.assertEqual(probe.select("hw", max_channels=2, max_rate=50000, run=run), probe.Format(2, 48000))

    def test_ceiling_below_minimum(self):
        run = fake({None: DUMP, 2: AT_2})
        with self.assertRaises(probe.ProbeError):
            probe.select("hw", max_channels=2, max_rate=100, run=run)

    def test_step_down(self):
        self.assertEqual(probe.step_down(48000), 44100)
        self.assertIsNone(probe.step_down(8000))


if __name__ == "__main__":
    unittest.main()
