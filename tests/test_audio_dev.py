import unittest
from types import SimpleNamespace
from typing import Any

from recall import audio_dev

HEADER = "**** List of CAPTURE Hardware Devices ****\n"


def card(n: int, cid: str, name: str, dev: int = 0) -> str:
    return (f"card {n}: {cid} [{name}], device {dev}: USB Audio [USB Audio]\n"
            "  Subdevices: 1/1\n  Subdevice #0: subdevice #0\n")


NONE = "arecord: device_list:279: no soundcards found...\n"
XR18 = HEADER + card(2, "XR18", "XR18")
BOTH = HEADER + card(0, "Headset", "USB Headset") + card(3, "XR18", "XR18")
TWO_XR = HEADER + card(4, "XR18_2", "XR18") + card(1, "XR18", "XR18")

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


def listing(text: str) -> Any:
    return lambda cmd, **kw: SimpleNamespace(stdout=text, stderr="")


def dumps(by_channels: dict[int | None, str]) -> Any:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> SimpleNamespace:
        calls.append(cmd)
        n = int(cmd[cmd.index("-c") + 1]) if "-c" in cmd else None
        return SimpleNamespace(stdout="", stderr=by_channels[n])

    run.calls = calls  # type: ignore[attr-defined]
    return run


class Bind(unittest.TestCase):
    def bound(self, patterns: list[str], text: str) -> audio_dev.Device | None:
        return audio_dev.get_target_device(patterns, listing(text))

    def test_no_match(self) -> None:
        self.assertIsNone(self.bound(["XR18"], NONE))
        self.assertIsNone(self.bound(["XR18"], BOTH.replace("XR18", "Other")))

    def test_one_match(self) -> None:
        d = self.bound(["XR18"], XR18)
        assert d is not None
        self.assertEqual((d.card, d.card_id, d.device), (2, "XR18", 0))
        self.assertEqual(d.hw, "hw:CARD=XR18,DEV=0")

    def test_pattern_order_beats_card_order(self) -> None:
        self.assertEqual(getattr(self.bound(["XR18", "Headset"], BOTH), "card_id"), "XR18")
        self.assertEqual(getattr(self.bound(["Headset", "XR18"], BOTH), "card_id"), "Headset")

    def test_lowest_card_wins_within_pattern(self) -> None:
        self.assertEqual(getattr(self.bound(["XR18"], TWO_XR), "card"), 1)

    def test_regex_search_on_full_line(self) -> None:
        self.assertEqual(getattr(self.bound([r"USB Audio \[USB"], XR18), "card"), 2)

    def test_empty_patterns_take_lowest_card(self) -> None:
        self.assertEqual(getattr(self.bound([], BOTH), "card"), 0)
        self.assertIsNone(self.bound([], NONE))


class Listing(unittest.TestCase):
    def test_runs_arecord_with_c_locale(self) -> None:
        seen: dict[str, Any] = {}

        def run(cmd: list[str], **kw: Any) -> Any:
            seen.update(cmd=cmd, lc=kw["env"]["LC_ALL"])
            return SimpleNamespace(stdout=XR18)

        self.assertEqual(len(audio_dev.list_devices(run)), 1)
        self.assertEqual((seen["cmd"], seen["lc"]), (["arecord", "-l"], "C"))

    def test_no_cards_is_empty(self) -> None:
        self.assertEqual(audio_dev.parse(NONE), [])


class Parse(unittest.TestCase):
    def test_ranges_and_single_values(self) -> None:
        self.assertEqual(audio_dev.parse_hw_params(DUMP), audio_dev.HwParams(audio_dev.Range(2, 18), audio_dev.Range(44100, 48000)))
        self.assertEqual(audio_dev.parse_hw_params(AT_18), audio_dev.HwParams(audio_dev.Range(18, 18), audio_dev.Range(48000, 48000)))

    def test_missing_fields(self) -> None:
        with self.assertRaises(audio_dev.ProbeError):
            audio_dev.parse_hw_params("nothing useful")


class Select(unittest.TestCase):
    def test_max_channels_then_rate_at_that_count(self) -> None:
        run = dumps({None: DUMP, 18: AT_18, 2: AT_2})
        self.assertEqual(audio_dev.resolve_device_format("hw:CARD=XR18,DEV=0", run=run), audio_dev.Format(18, 48000))
        self.assertIn("-D", run.calls[0])
        self.assertIn("hw:CARD=XR18,DEV=0", run.calls[0])

    def test_channel_ceiling_changes_rate_lookup(self) -> None:
        run = dumps({None: DUMP, 18: AT_18, 2: AT_2})
        self.assertEqual(audio_dev.resolve_device_format("hw", max_channels=2, run=run), audio_dev.Format(2, 96000))

    def test_rate_ceiling(self) -> None:
        run = dumps({None: DUMP, 2: AT_2})
        self.assertEqual(audio_dev.resolve_device_format("hw", max_channels=2, max_rate=50000, run=run),
                         audio_dev.Format(2, 48000))

    def test_ceiling_below_minimum(self) -> None:
        run = dumps({None: DUMP, 2: AT_2})
        with self.assertRaises(audio_dev.ProbeError):
            audio_dev.resolve_device_format("hw", max_channels=2, max_rate=100, run=run)

    def test_step_down(self) -> None:
        self.assertEqual(audio_dev.step_down(48000), 44100)
        self.assertIsNone(audio_dev.step_down(8000))


if __name__ == "__main__":
    unittest.main()
