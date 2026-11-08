"""Recording loop: bind the device, run arecord in chunks, detect loss and stall, rebind."""

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from recall import __version__, audio_dev, config, display, health, storage

log = logging.getLogger("recall")

TICK = 1.0  # seconds between checks; also the telemetry interval
CHUNK_LIMIT = 1_900_000_000  # arecord stops writing a WAV near 1.98 GB and does not split it: stay under that
START_GRACE = 2.0  # arecord exiting sooner than this after spawn means the open failed
STOP_TIMEOUT = 5.0  # time arecord gets to close its open chunk after SIGTERM, before SIGKILL

Spawn = Callable[..., Any]
Clock = Callable[[], float]


def slugify(card_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", card_id)


def byte_rate(channels: int, rate: int, bit_depth: int) -> int:
    return channels * rate * bit_depth // 8


def chunk_length(configured: int, channels: int, rate: int, bit_depth: int) -> int:
    """Shorter of the configured chunk and the longest that stays under the arecord WAV limit."""
    return min(configured, CHUNK_LIMIT // byte_rate(channels, rate, bit_depth))


def next_session(output_dir: str, slug: str) -> int:
    """One more than the highest session number already on the drive for this device."""
    rx = re.compile(rf"^{re.escape(slug)}-(\d{{4}})(?:-\d+)?\.wav$")
    found = [int(m[1]) for m in map(rx.match, os.listdir(output_dir)) if m]
    return max(found, default=0) + 1


def capture_command(hw: str, fmt: audio_dev.Format, chunk: int, out: str) -> list[str]:
    """arecord takes the rate and depth as asked, unlike sox's ALSA input, which ignores `-r`.

    `out` is `name.wav`. arecord writes that name first, then `name-01.wav`, `name-02.wav`, ... after
    each `chunk` seconds.
    """
    return ["arecord", "-q", "-D", hw, "-c", str(fmt.channels), "-r", str(fmt.rate),
            "-f", audio_dev.FORMAT_NAMES[fmt.bits], "-t", "wav", "--max-file-time", str(chunk), out]


@dataclass
class Session:
    proc: Any
    device: audio_dev.Device
    fmt: audio_dev.Format
    slug: str
    number: int
    base: str
    started: float
    chunk_name: str = ""
    chunk_started: float = 0.0  # when the current chunk file first appeared
    closed_bytes: int = 0  # bytes in the chunks this session has finished
    size: int = 0
    grew: float = 0.0


class Recorder:
    def __init__(self, cfg: config.Config, run: audio_dev.Run = subprocess.run,
                 spawn: Spawn = subprocess.Popen, clock: Clock = time.monotonic,
                 screen: display.Display | None = None, vitals: health.Health | None = None) -> None:
        self.cfg = cfg
        self.screen = screen
        self.vitals = vitals or health.Health(clock)
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
        self.report()

    def report(self, state: str | None = None) -> None:
        """One `key=value` status record: the telemetry the journal and the display read."""
        s = self.session
        if state is None:
            state = "recording" if s else "waiting"
        now = self.clock()
        fields: dict[str, object] = {
            "state": state,
            "session_s": int(now - s.started) if s else 0,
            "chunk_s": int(now - s.chunk_started) if s else 0,
            "free_bytes": shutil.disk_usage(self.cfg.output_dir).free,
        }
        if s:
            per_second = byte_rate(s.fmt.channels, s.fmt.rate, s.fmt.bits)
            fields.update(device=s.device.hw, channels=s.fmt.channels, rate=s.fmt.rate, bits=s.fmt.bits,
                          chunk_length=chunk_length(self.cfg.chunk_seconds, s.fmt.channels, s.fmt.rate, s.fmt.bits),
                          remaining_s=fields["free_bytes"] // per_second,
                          file=s.chunk_name, file_bytes=s.size, session_bytes=s.closed_bytes + s.size)
            fields.update(self.vitals.sample(s.closed_bytes + s.size, per_second))
        log.info("status %s", " ".join(f"{k}={v}" for k, v in fields.items()))
        if self.screen:
            if s is None:  # nothing to record yet: tell the operator what is wanted and what is there
                seen = dict.fromkeys(d.card_id for d in audio_dev.list_devices(self.run))
                fields.update(want=list(self.cfg.match), have=list(seen))
            self.screen.show(fields)

    def try_start(self) -> None:
        dev = audio_dev.get_target_device(self.cfg.match, self.run)
        if dev is None:
            return
        try:
            fmt = audio_dev.resolve_device_format(dev.hw, self.cfg.max_channels, self.rate_cap,
                                                  self.cfg.bit_depth, self.run)
        except audio_dev.ProbeError as e:
            self.note(f"{dev.hw}: {e}")
            return
        self.start(dev, fmt)

    def start(self, dev: audio_dev.Device, fmt: audio_dev.Format) -> None:
        slug = slugify(dev.card_id)
        number = next_session(self.cfg.output_dir, slug)
        chunk = chunk_length(self.cfg.chunk_seconds, fmt.channels, fmt.rate, fmt.bits)
        base = f"{slug}-{number:04d}"
        out = os.path.join(self.cfg.output_dir, f"{base}.wav")
        cmd = capture_command(dev.hw, fmt, chunk, out)
        self.vitals.reset()
        proc = self.spawn(cmd, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
        if getattr(proc, "stderr", None):
            threading.Thread(target=self.read_stderr, args=(proc.stderr,), daemon=True).start()
        now = self.clock()
        self.session = Session(proc, dev, fmt, slug, number, base, started=now, grew=now)
        self.last_problem = ""
        log.info("recording %s: %d ch, %d Hz, %d-bit, session %04d, %d s chunks",
                 dev.hw, fmt.channels, fmt.rate, fmt.bits, number, chunk)

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
            return self.lose(f"arecord exited with status {rc}")

        latest = self.latest_chunk(s)
        if latest != s.chunk_name:
            if s.chunk_name:
                os.sync()  # the previous chunk has closed: push it to the drive
                s.closed_bytes += os.path.getsize(os.path.join(self.cfg.output_dir, s.chunk_name))
            s.chunk_name = latest
            s.chunk_started = now
            s.size, s.grew = 0, now  # a new chunk starts small: measure its growth from zero

        size = os.path.getsize(os.path.join(self.cfg.output_dir, latest)) if latest else 0
        if size > s.size:
            s.size, s.grew = size, now
        elif now - s.grew >= self.cfg.stall_seconds:
            return self.lose(f"output stopped growing for {self.cfg.stall_seconds:g} s")

    def read_stderr(self, stream: Any) -> None:
        """Log what arecord says and count its overruns (it prints `overrun!!!` even with -q). Ends at EOF."""
        for line in stream:
            line = line.strip()
            if "overrun" in line.lower():
                if self.vitals.overrun():  # a burst is logged once, not once per overrun
                    log.warning("arecord: %s (%d overruns this session)", line, self.vitals.overruns)
            elif line:
                log.warning("arecord: %s", line)

    def open_failed(self, s: Session) -> None:
        nxt = audio_dev.step_down(s.fmt.rate)
        self.lose(f"could not open {s.device.hw} at {s.fmt.rate} Hz")
        if nxt is None:
            log.error("no lower rate to try for %s", s.device.hw)
        else:
            self.rate_cap = nxt

    def lose(self, reason: str) -> None:
        log.warning("device lost: %s; rescanning", reason)
        self.stop_capture()
        os.sync()
        self.session = None
        self.rate_cap = self.cfg.max_rate
        self.report("device lost")  # a state change is reported now, not at the next tick

    def latest_chunk(self, s: Session) -> str:
        rx = re.compile(rf"^{re.escape(s.base)}(?:-(\d+))?\.wav$")
        found = [(int(m[1] or 0), m[0]) for m in map(rx.match, os.listdir(self.cfg.output_dir)) if m]
        return max(found, default=(0, ""))[1]

    def note(self, problem: str) -> None:
        if problem != self.last_problem:  # log a repeated wait reason once, not every tick
            self.last_problem = problem
            log.warning("cannot start: %s", problem)

    def stop_capture(self) -> None:
        if self.session is None or self.session.proc.poll() is not None:
            return
        proc = self.session.proc
        proc.terminate()  # arecord finalizes the open chunk on SIGTERM; SIGKILL would leave it unreadable
        try:
            proc.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def stop(self) -> None:
        self.stop_capture()
        os.sync()
        self.session = None


def run(cfg: config.Config) -> None:
    """Record until the output directory disappears; raises storage.StorageError.

    SIGINT and SIGTERM both end in the `finally`, so arecord gets to close its chunk.
    """
    screen = display.Display() if cfg.display else None
    rec = Recorder(cfg, screen=screen)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))  # a stop, not a failure
    log.info("recall %s", __version__)
    try:
        while True:
            rec.tick()
            time.sleep(TICK)
    finally:
        rec.stop()
        if screen:
            screen.clear()
