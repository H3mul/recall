import unittest
from types import SimpleNamespace

from recall import discovery

HEADER = "**** List of CAPTURE Hardware Devices ****\n"


def card(n, cid, name, dev=0):
    return (f"card {n}: {cid} [{name}], device {dev}: USB Audio [USB Audio]\n"
            "  Subdevices: 1/1\n  Subdevice #0: subdevice #0\n")


NONE = "arecord: device_list:279: no soundcards found...\n"
XR18 = HEADER + card(2, "XR18", "XR18")
BOTH = HEADER + card(0, "Headset", "USB Headset") + card(3, "XR18", "XR18")
TWO_XR = HEADER + card(4, "XR18_2", "XR18") + card(1, "XR18", "XR18")


class Bind(unittest.TestCase):
    def bound(self, patterns, text):
        return discovery.bind(patterns, discovery.parse(text))

    def test_no_match(self):
        self.assertIsNone(self.bound(["XR18"], NONE))
        self.assertIsNone(self.bound(["XR18"], BOTH.replace("XR18", "Other")))

    def test_one_match(self):
        d = self.bound(["XR18"], XR18)
        self.assertEqual((d.card, d.card_id, d.device), (2, "XR18", 0))
        self.assertEqual(d.hw, "hw:CARD=XR18,DEV=0")

    def test_pattern_order_beats_card_order(self):
        self.assertEqual(self.bound(["XR18", "Headset"], BOTH).card_id, "XR18")
        self.assertEqual(self.bound(["Headset", "XR18"], BOTH).card_id, "Headset")

    def test_lowest_card_wins_within_pattern(self):
        self.assertEqual(self.bound(["XR18"], TWO_XR).card, 1)

    def test_regex_search_on_full_line(self):
        self.assertEqual(self.bound([r"USB Audio \[USB"], XR18).card, 2)


class Listing(unittest.TestCase):
    def test_runs_arecord_with_c_locale(self):
        seen = {}

        def run(cmd, **kw):
            seen.update(cmd=cmd, lc=kw["env"]["LC_ALL"])
            return SimpleNamespace(stdout=XR18)

        self.assertEqual(len(discovery.list_devices(run)), 1)
        self.assertEqual((seen["cmd"], seen["lc"]), (["arecord", "-l"], "C"))

    def test_no_cards_is_empty(self):
        self.assertEqual(discovery.parse(NONE), [])


if __name__ == "__main__":
    unittest.main()
