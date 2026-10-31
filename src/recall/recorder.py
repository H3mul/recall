"""Recording loop: bind the device, run SoX in chunks, detect loss and stall, rebind."""

import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from recall import audio_dev, config, storage

log = logging.getLogger("recall")

TICK = 1.0  # seconds between checks; also the telemetry interval
CHUNK_LIMIT = 4_000_000_000  # keeps each WAV under the RIFF 4 GB limit
START_GRACE = 2.0  # SoX exiting sooner than this after spawn means the open failed
SOX_STOP_TIMEOUT = 5.0  # time SoX gets to close its open chunk after SIGTERM, before SIGKILL

Spawn = Callable[..., Any]
Clock = Callable[[], float]


def slugify(card_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", card_id)


def byte_rate(channels: int, rate: int, bit_depth: int) -> int:
    return channels * rate * bit_depth // 8


def chunk_length(configured: int, channels: int, rate: int, bit_depth: int) -> int:
    """Shorter of the configured chunk and the longest that stays under the RIFF limit."""
    return min(configured, CHUNK_LIMIT // byte_rate(channels, rate, bit_depth))


def next_session(output_dir: str, slug: str) -> int:
    """One more than the highest session number already on the drive for this device."""
    rx = re.compile(rf"^{re.escape(slug)}-(\d{{4}})-\d+\.wav$")
    found = [int(m[1]) for m in map(rx.match, os.listdir(output_dir)) if m]
    return max(found, default=0) + 1


def sox_command(hw: str, fmt: audio_dev.Format, bit_depth: int, chunk: int, out: str) -> list[str]:
    """SoX splits on `trim`/`newfile`/`restart`. `out` carries the `-` that SoX fills with the chunk number."""
    return ["sox", "--no-clobber", "-t", "alsa", hw, "-c", str(fmt.channels), "-r", str(fmt.rate),
            "-t", "wav", "-b", str(bit_depth), out,
            "trim", "0", str(chunk), ":", "newfile", ":", "restart"]


@dataclass
class Session:
    proc: Any
    device: audio_dev.Device
    fmt: audio_dev.Format
    slug: str
    number: int
    prefix: str
    started: float
    chunk_name: str = ""
    size: int = 0
    grew: float = 0.0


class Recorder:
    def __init__(self, cfg: config.Config, run: audio_dev.Run = subprocess.run,
                 spawn: Spawn = subprocess.Popen, clock: Clock = time.monotonic) -> None:
        self.cfg = cfg
        self.run = run
        self.spawn = spawn
        self.clock = clock
        self.session: Session | None = None
        self.rate_cap = cfg.max_rate  # lowered after an open failure, reset on device loss
        self.last_problem = ""

    def tick(self) -> None:
        storage.verify(self.cfg.output_dir)  # raises StorageError: the caller stops with 69
        if self.session is None:
            self.try_start()
        else:
            self.watch()

    def try_start(self) -> None:
        dev = audio_dev.get_target_device(self.cfg.match, self.run)
        if dev is None:
            return
        try:
            fmt = audio_dev.resolve_device_format(dev.hw, self.cfg.max_channels, self.rate_cap, self.run)
        except audio_dev.ProbeError as e:
            self.note(f"{dev.hw}: {e}")
            return
        self.start(dev, fmt)

    def start(self, dev: audio_dev.Device, fmt: audio_dev.Format) -> None:
        slug = slugify(dev.card_id)
        number = next_session(self.cfg.output_dir, slug)
        chunk = chunk_length(self.cfg.chunk_seconds, fmt.channels, fmt.rate, self.cfg.bit_depth)
        prefix = f"{slug}-{number:04d}-"
        out = os.path.join(self.cfg.output_dir, f"{prefix}.wav")
        cmd = sox_command(dev.hw, fmt, self.cfg.bit_depth, chunk, out)
        proc = self.spawn(cmd, stdin=subprocess.DEVNULL)
        now = self.clock()
        self.session = Session(proc, dev, fmt, slug, number, prefix, started=now, grew=now)
        self.last_problem = ""
        log.info("recording %s: %d ch, %d Hz, session %04d, %d s chunks",
                 dev.hw, fmt.channels, fmt.rate, number, chunk)

    def watch(self) -> None:
        s = self.session
        assert s is not None
        now = self.clock()

        if not any(d.hw == s.device.hw for d in audio_dev.list_devices(self.run)):
            return self.lose("device no longer listed")

        rc = s.proc.poll()
        if rc is not None:
            if rc != 0 and now - s.started < START_GRACE:
                return self.open_failed(s)
            return self.lose(f"sox exited with status {rc}")

        latest = self.latest_chunk(s)
        if latest != s.chunk_name:
            if s.chunk_name:
                os.sync()  # the previous chunk has closed: push it to the drive
            s.chunk_name = latest
            s.size, s.grew = 0, now  # a new chunk starts small: measure its growth from zero

        size = os.path.getsize(os.path.join(self.cfg.output_dir, latest)) if latest else 0
        if size > s.size:
            s.size, s.grew = size, now
        elif now - s.grew >= self.cfg.stall_seconds:
            return self.lose(f"output stopped growing for {self.cfg.stall_seconds:g} s")

    def open_failed(self, s: Session) -> None:
        nxt = audio_dev.step_down(s.fmt.rate)
        self.lose(f"sox could not open {s.device.hw} at {s.fmt.rate} Hz")
        if nxt is None:
            log.error("no lower rate to try for %s", s.device.hw)
        else:
            self.rate_cap = nxt

    def lose(self, reason: str) -> None:
        log.warning("device lost: %s; rescanning", reason)
        self.stop_sox()
        os.sync()
        self.session = None
        self.rate_cap = self.cfg.max_rate

    def latest_chunk(self, s: Session) -> str:
        names = [n for n in os.listdir(self.cfg.output_dir)
                 if n.startswith(s.prefix) and n.endswith(".wav")]
        return max(names, key=lambda n: int(n[len(s.prefix):-4]), default="")

    def note(self, problem: str) -> None:
        if problem != self.last_problem:  # log a repeated wait reason once, not every tick
            self.last_problem = problem
            log.warning("cannot start: %s", problem)

    def stop_sox(self) -> None:
        if self.session is None or self.session.proc.poll() is not None:
            return
        proc = self.session.proc
        proc.terminate()  # SoX finalizes the open chunk on SIGTERM; SIGKILL would leave it unreadable
        try:
            proc.wait(timeout=SOX_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def stop(self) -> None:
        self.stop_sox()
        os.sync()
        self.session = None


def run(cfg: config.Config) -> None:
    """Record until the output directory disappears; raises storage.StorageError.

    SIGINT and SIGTERM both end in the `finally`, so SoX gets to close its chunk.
    """
    rec = Recorder(cfg)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))  # a stop, not a failure
    try:
        while True:
            rec.tick()
            time.sleep(TICK)
    finally:
        rec.stop()
