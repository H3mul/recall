"""Recorder health readings: write rate, IO stall, write backlog and overruns, over short windows."""

import time
from collections import deque
from collections.abc import Callable
from typing import Any

WINDOW = 30.0  # seconds an overrun alert or slow-drive warning stays up
RATE_WINDOW = 5.0  # seconds the write rate is averaged over
LOG_QUIET = 5.0  # seconds without an overrun before the next one is logged again
# The kernel normally holds about 30 s of fresh writes (dirty_expire_centisecs) before flushing them.
# A backlog beyond this many seconds of recording data means the drive is falling behind.
BACKLOG_SECONDS = 45


def read_proc(path: str) -> str:
    with open(path) as f:
        return f.read()


def io_stall_pct(pressure: str) -> float:
    """The 10 s average share of time some task was stalled on IO, from /proc/pressure/io."""
    some = next(line for line in pressure.splitlines() if line.startswith("some"))
    return float(dict(field.split("=") for field in some.split()[1:])["avg10"])


def backlog_bytes(meminfo: str) -> int:
    """Data accepted by the kernel but not yet on the drive: Dirty plus Writeback."""
    kb = {line.split(":")[0]: int(line.split()[1]) for line in meminfo.splitlines() if line}
    return (kb["Dirty"] + kb["Writeback"]) * 1024


class Health:
    """Per-session readings. `overrun()` may be called from another thread."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 read: Callable[[str], str] = read_proc) -> None:
        self.clock, self.read = clock, read
        self.reset()

    def reset(self) -> None:
        self.writes: deque[tuple[float, int]] = deque()  # (time, session bytes written)
        self.overruns = 0
        self.overrun_at: float | None = None
        self.slow_at: float | None = None  # last time the write backlog was over the limit

    def overrun(self) -> bool:
        """Count one overrun. True if it is worth logging: none came in the last few seconds."""
        now = self.clock()
        quiet = self.overrun_at is None or now - self.overrun_at >= LOG_QUIET
        self.overruns += 1
        self.overrun_at = now
        return quiet

    def sample(self, session_bytes: int, bytes_per_second: int) -> dict[str, Any]:
        """Fields for the status record. Readings the system cannot give are left out."""
        now = self.clock()
        self.writes.append((now, session_bytes))
        while now - self.writes[0][0] > RATE_WINDOW:
            self.writes.popleft()

        fields: dict[str, Any] = {"overruns": self.overruns}
        (t0, b0), (t1, b1) = self.writes[0], self.writes[-1]
        if t1 > t0:
            fields["write_bps"] = int((b1 - b0) / (t1 - t0))
        fields["overrun_recent"] = int(self.overrun_at is not None and now - self.overrun_at < WINDOW)

        try:
            fields["io_stall_pct"] = io_stall_pct(self.read("/proc/pressure/io"))
        except (OSError, StopIteration, KeyError, ValueError):  # no PSI in this kernel
            pass
        try:
            fields["backlog_bytes"] = backlog_bytes(self.read("/proc/meminfo"))
            if fields["backlog_bytes"] > BACKLOG_SECONDS * bytes_per_second:
                self.slow_at = now
        except (OSError, KeyError, ValueError, IndexError):
            pass
        fields["behind"] = int(self.slow_at is not None and now - self.slow_at < WINDOW)
        return fields
