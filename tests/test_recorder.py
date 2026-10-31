import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any

from recall import audio_dev, config, recorder, storage

HEADER = "**** List of CAPTURE Hardware Devices ****\n"
XR18 = HEADER + "card 2: XR18 [XR18], device 0: USB Audio [USB Audio]\n"
DUMP_ALL = "CHANNELS: [2 18]\nRATE: [44100 48000]\n"  # no channel count given
DUMP_18 = "CHANNELS: 18\nRATE: [48000 48000]\n"  # at 18 channels only 48 kHz


class FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.killed = True  # SoX closes its chunk on SIGTERM; the fake just exits
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.listing = XR18
        self.spawned: list[tuple[list[str], FakeProc]] = []
        self.now = 0.0
        self.cfg = config.Config(match=("XR18",), output_dir=self.dir.name, max_channels=18,
                                 chunk_seconds=900, stall_seconds=10)
        self.rec = recorder.Recorder(self.cfg, run=self.arecord, spawn=self.spawn, clock=lambda: self.now)

    def arecord(self, cmd: list[str], **_: Any) -> SimpleNamespace:
        if "-l" in cmd:
            return SimpleNamespace(stdout=self.listing, stderr="")
        dump = DUMP_18 if "-c" in cmd else DUMP_ALL
        return SimpleNamespace(stdout=dump, stderr="")

    def spawn(self, cmd: list[str], **_: Any) -> FakeProc:
        proc = FakeProc()
        self.spawned.append((cmd, proc))
        return proc

    def out_of(self, i: int) -> str:
        # SoX's output path is the 8th argument from the end
        return os.path.basename(self.spawned[i][0][-8])

    def grow(self, name: str, size: int) -> None:
        with open(os.path.join(self.dir.name, name), "wb") as f:
            f.write(b"\0" * size)


class Naming(unittest.TestCase):
    def test_first_session_on_empty_drive(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(recorder.next_session(d, "XR18"), 1)

    def test_later_session_ignores_other_devices(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for n in ("XR18-0007-003.wav", "Headset-0009-001.wav", "XR18-0002-001.txt"):
                open(os.path.join(d, n), "wb").close()
            self.assertEqual(recorder.next_session(d, "XR18"), 8)

    def test_chunk_length_capped_by_riff_limit(self) -> None:
        self.assertEqual(recorder.chunk_length(900, 18, 48000, 24), 900)
        self.assertEqual(recorder.chunk_length(900, 18, 96000, 24), 771)

    def test_sox_command_splits_and_restarts(self) -> None:
        fmt = audio_dev.Format(18, 48000)
        cmd = recorder.sox_command("hw:CARD=XR18,DEV=0", fmt, 24, 900, "/d/XR18-0001-.wav")
        self.assertEqual(cmd[-7:], ["trim", "0", "900", ":", "newfile", ":", "restart"])
        self.assertIn("--no-clobber", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "18")


class Supervision(Harness):
    def test_binds_and_starts_sox_with_next_session(self) -> None:
        self.rec.tick()
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.out_of(0), "XR18-0001-.wav")
        self.assertIsNotNone(self.rec.session)

    def test_device_loss_kills_sox_and_rebinds_as_next_session(self) -> None:
        self.rec.tick()
        proc = self.spawned[0][1]
        self.grow("XR18-0001-001.wav", 100)  # session 1 is on the drive
        self.listing = HEADER  # unplugged
        self.now += 1
        self.rec.tick()
        self.assertTrue(proc.killed)
        self.assertIsNone(self.rec.session)
        self.listing = XR18  # back
        self.rec.tick()
        self.assertEqual(self.out_of(1), "XR18-0002-.wav")

    def test_stall_rebinds_after_stall_seconds(self) -> None:
        self.rec.tick()
        proc = self.spawned[0][1]
        self.grow("XR18-0001-001.wav", 100)
        self.now = 1
        self.rec.tick()  # growth seen at t=1
        self.now = 10
        self.rec.tick()  # 9 s without growth: still recording
        self.assertIsNotNone(self.rec.session)
        self.now = 11
        self.rec.tick()  # 10 s without growth: stalled
        self.assertTrue(proc.killed)
        self.assertIsNone(self.rec.session)

    def test_chunk_rollover_is_not_a_stall(self) -> None:
        self.rec.tick()
        proc = self.spawned[0][1]
        self.grow("XR18-0001-001.wav", 1000)
        self.now = 1
        self.rec.tick()
        self.grow("XR18-0001-002.wav", 10)  # the next chunk starts smaller than the last one
        self.now = 9
        self.rec.tick()  # rollover seen at t=9
        self.now = 18
        self.rec.tick()  # 9 s into the new chunk: still healthy
        self.assertFalse(proc.killed)
        self.now = 19
        self.rec.tick()  # 10 s without growth in the new chunk: stalled
        self.assertTrue(proc.killed)

    def test_open_failure_steps_rate_down(self) -> None:
        self.rec.tick()
        self.spawned[0][1].returncode = 1  # exits before the grace period ends
        self.rec.tick()
        self.assertIsNone(self.rec.session)
        self.assertEqual(self.rec.rate_cap, 44100)

    def test_drive_removed_while_recording_raises_storage_error(self) -> None:
        drive = os.path.join(self.dir.name, "drive")
        os.mkdir(drive)
        self.rec.cfg = config.Config(match=("XR18",), output_dir=drive, max_channels=18)
        self.rec.tick()
        self.assertIsNotNone(self.rec.session)
        shutil.rmtree(drive)
        with self.assertRaises(storage.StorageError):
            self.rec.tick()
        self.rec.stop()
        self.assertTrue(self.spawned[0][1].killed)


if __name__ == "__main__":
    unittest.main()
