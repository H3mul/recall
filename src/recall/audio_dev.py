"""Capture device discovery (`arecord -l`) and format introspection (`arecord --dump-hw-params`)."""

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

LINE = re.compile(r"^card (\d+): (\S+) \[(.*?)\], device (\d+): ")
STANDARD_RATES = (192000, 176400, 96000, 88200, 48000, 44100, 32000, 22050, 16000, 8000)
RANGE = re.compile(r"^(CHANNELS|RATE):\s*[\[(]?\s*(\d+)(?:\s+(\d+))?\s*[\])]?\s*$", re.M)

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class ProbeError(Exception):
    pass


@dataclass(frozen=True)
class Device:
    card: int
    card_id: str
    device: int
    line: str

    @property
    def hw(self) -> str:
        return f"hw:CARD={self.card_id},DEV={self.device}"


@dataclass(frozen=True)
class Format:
    channels: int
    rate: int


@dataclass(frozen=True)
class Range:
    lo: int
    hi: int


@dataclass(frozen=True)
class HwParams:
    channels: Range
    rate: Range


def parse(text: str) -> list[Device]:
    """Parse `arecord -l` output into Devices, in the order listed."""
    found = []
    for line in text.splitlines():
        m = LINE.match(line)
        if m:
            found.append(Device(int(m[1]), m[2], int(m[4]), line))
    return found


def list_devices(run: Run = subprocess.run) -> list[Device]:
    """Run `arecord -l` under LC_ALL=C. No sound cards is an empty list, not an error."""
    r = run(["arecord", "-l"], capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"})
    return parse(r.stdout)


def get_target_device(patterns: Sequence[str], run: Run = subprocess.run) -> Device | None:
    """First pattern in list order wins; within a pattern, the lowest card number (then device).

    With no patterns, the lowest-numbered device is used.
    """
    ordered = sorted(list_devices(run), key=lambda d: (d.card, d.device))
    if not patterns:
        return ordered[0] if ordered else None
    for pattern in patterns:
        rx = re.compile(pattern)
        for d in ordered:
            if rx.search(d.line):
                return d
    return None


def parse_hw_params(text: str) -> HwParams:
    """Read the CHANNELS and RATE spans from a hw-params dump."""
    out: dict[str, Range] = {}
    for m in RANGE.finditer(text):
        lo = int(m[2])
        out[m[1]] = Range(lo, int(m[3]) if m[3] else lo)
    missing = {"CHANNELS", "RATE"} - out.keys()
    if missing:
        raise ProbeError(f"hw-params dump has no {', '.join(sorted(missing))}")
    return HwParams(out["CHANNELS"], out["RATE"])


def arecord_dump_hw_params_raw(hw: str, channel_count: int | None = None, run: Run = subprocess.run) -> str:
    """Run the dump for `hw`, optionally restricted to `channel_count` (the rate range depends on it)."""
    cmd = ["arecord", "--dump-hw-params", "-D", hw, "-d", "1"]
    if channel_count:
        cmd += ["-c", str(channel_count)]
    r = run(cmd + ["/dev/null"], capture_output=True, text=True,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    # arecord prints the dump on stderr in some versions, stdout in others
    return r.stdout + r.stderr


def best_rate(lo: int, hi: int, max: int | None = None) -> int:
    """Highest usable rate in [lo, hi] not above `ceiling`."""
    top = min(hi, max) if max else hi
    if top < lo:
        raise ProbeError(f"rate ceiling {max} is below the device minimum {lo}")
    if top == hi:
        return hi
    # a continuous range may not accept an arbitrary value: prefer a standard rate
    return next((r for r in STANDARD_RATES if lo <= r <= top), top)


def step_down(rate: int, floor: int = 0) -> int | None:
    """Next lower standard rate, or None. Used when opening at `rate` fails."""
    return next((r for r in STANDARD_RATES if floor <= r < rate), None)


def resolve_device_format(hw: str, max_channels: int | None = None, max_rate: int | None = None,
           run: Run = subprocess.run) -> Format:
    """Most channels, then the highest rate at that channel count, within the ceilings."""
    params = parse_hw_params(arecord_dump_hw_params_raw(hw, run=run))
    chans = params.channels
    channels = min(chans.hi, max_channels) if max_channels else chans.hi
    if channels < chans.lo:
        raise ProbeError(f"channel ceiling {max_channels} is below the device minimum {chans.lo}")
    # the rate range can depend on the channel count, so ask again at the chosen count
    rate = parse_hw_params(arecord_dump_hw_params_raw(hw, channels, run=run)).rate
    return Format(channels, best_rate(rate.lo, rate.hi, max_rate))
