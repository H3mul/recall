"""Capture device discovery from `arecord -l`."""

import os
import re
import subprocess
from dataclasses import dataclass

LINE = re.compile(r"^card (\d+): (\S+) \[(.*?)\], device (\d+): ")


@dataclass(frozen=True)
class Device:
    card: int
    card_id: str
    device: int
    line: str

    @property
    def hw(self):
        return f"hw:CARD={self.card_id},DEV={self.device}"


def parse(text):
    """Parse `arecord -l` output into Devices, in the order listed."""
    found = []
    for line in text.splitlines():
        m = LINE.match(line)
        if m:
            found.append(Device(int(m[1]), m[2], int(m[4]), line))
    return found


def bind(patterns, devices):
    """First pattern in list order wins; within a pattern, the lowest card number (then device)."""
    ordered = sorted(devices, key=lambda d: (d.card, d.device))
    for pattern in patterns:
        rx = re.compile(pattern)
        for d in ordered:
            if rx.search(d.line):
                return d
    return None


def list_devices(run=subprocess.run):
    """Run `arecord -l` under LC_ALL=C. No sound cards is an empty list, not an error."""
    r = run(["arecord", "-l"], capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"})
    return parse(r.stdout)
