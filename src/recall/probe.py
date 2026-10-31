"""Format introspection from `arecord --dump-hw-params`."""

import re
import subprocess
from dataclasses import dataclass

STANDARD_RATES = (192000, 176400, 96000, 88200, 48000, 44100, 32000, 22050, 16000, 8000)
RANGE = re.compile(r"^(CHANNELS|RATE):\s*[\[(]?\s*(\d+)(?:\s+(\d+))?\s*[\])]?\s*$", re.M)


class ProbeError(Exception):
    pass


@dataclass(frozen=True)
class Format:
    channels: int
    rate: int


def parse_ranges(text):
    """Return {"CHANNELS": (lo, hi), "RATE": (lo, hi)} from a hw-params dump."""
    out = {}
    for m in RANGE.finditer(text):
        lo = int(m[2])
        out[m[1]] = (lo, int(m[3]) if m[3] else lo)
    missing = {"CHANNELS", "RATE"} - out.keys()
    if missing:
        raise ProbeError(f"hw-params dump has no {', '.join(sorted(missing))}")
    return out


def dump(hw, channels=None, run=subprocess.run):
    """Run the dump for `hw`, optionally restricted to `channels` (rate range depends on it)."""
    cmd = ["arecord", "--dump-hw-params", "-D", hw, "-d", "1"]
    if channels:
        cmd += ["-c", str(channels)]
    r = run(cmd + ["/dev/null"], capture_output=True, text=True, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    # arecord prints the dump on stderr in some versions, stdout in others
    return r.stdout + r.stderr


def best_rate(lo, hi, ceiling=None):
    """Highest usable rate in [lo, hi] not above `ceiling`."""
    top = min(hi, ceiling) if ceiling else hi
    if top < lo:
        raise ProbeError(f"rate ceiling {ceiling} is below the device minimum {lo}")
    if top == hi:
        return hi
    # a continuous range may not accept an arbitrary value: prefer a standard rate
    return next((r for r in STANDARD_RATES if lo <= r <= top), top)


def step_down(rate, floor=0):
    """Next lower standard rate, or None. Used when opening at `rate` fails."""
    return next((r for r in STANDARD_RATES if floor <= r < rate), None)


def select(hw, max_channels=None, max_rate=None, run=subprocess.run):
    """Most channels, then the highest rate at that channel count, within the ceilings."""
    lo, hi = parse_ranges(dump(hw, run=run))["CHANNELS"]
    channels = min(hi, max_channels) if max_channels else hi
    if channels < lo:
        raise ProbeError(f"channel ceiling {max_channels} is below the device minimum {lo}")
    rlo, rhi = parse_ranges(dump(hw, channels, run=run))["RATE"]
    return Format(channels, best_rate(rlo, rhi, max_rate))
