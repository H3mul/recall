import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any

from recall import audio_dev, config, recorder, storage

HEADER = "**** List of CAPTURE Hardware Devices ****\n"
XR18 = HEADER + "card 2: XR18 [XR18], device 0: USB Audio [USB Audio]\n"
DUMP_ALL = "CHANNELS: [2 18]\nRATE: [44100 48000]\nFORMAT:  S24_3LE\n"  # no channel count given
DUMP_18 = "CHANNELS: 18\nRATE: [48000 48000]\nFORMAT:  S24_3LE\n"  # at 18 channels only 48 kHz


class FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.killed = True  # arecord closes its chunk on SIGTERM; the fake just exits
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
        # arecord's output path is the last argument
        return os.path.basename(self.spawned[i][0][-1])

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

    def test_plain_first_chunk_counts_as_its_session(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "XR18-0006.wav"), "wb").close()  # arecord's first chunk has no -NN
            self.assertEqual(recorder.next_session(d, "XR18"), 7)

    def test_latest_chunk_is_highest_number_not_last_string(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for n in ("XR18-0006.wav", "XR18-0006-01.wav", "XR18-0006-02.wav", "XR18-0006-10.wav"):
                open(os.path.join(d, n), "wb").close()
            rec = recorder.Recorder(config.Config(match=(), output_dir=d))
            s = recorder.Session(None, None, None, "XR18", 6, "XR18-0006", started=0.0)  # type: ignore[arg-type]
            self.assertEqual(rec.latest_chunk(s), "XR18-0006-10.wav")

    def test_chunk_length_capped_by_riff_limit(self) -> None:
        self.assertEqual(recorder.chunk_length(900, 18, 48000, 24), 900)
        self.assertEqual(recorder.chunk_length(900, 18, 96000, 24), 771)

    def test_capture_command_splits_by_time_at_asked_format(self) -> None:
        fmt = audio_dev.Format(18, 48000, 24)
        cmd = recorder.capture_command("hw:CARD=XR18,DEV=0", fmt, 900, "/d/XR18-0001.wav")
        self.assertEqual(cmd[cmd.index("-r") + 1], "48000")
        self.assertEqual(cmd[cmd.index("-f") + 1], "S24_3LE")
        self.assertEqual(cmd[cmd.index("-c") + 1], "18")
        self.assertEqual(cmd[cmd.index("--max-file-time") + 1], "900")
        self.assertEqual(cmd[-1], "/d/XR18-0001.wav")


class Supervision(Harness):
    def test_binds_and_starts_capture_with_next_session(self) -> None:
        self.rec.tick()
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.out_of(0), "XR18-0001.wav")
        self.assertIsNotNone(self.rec.session)

    def test_device_loss_kills_capture_and_rebinds_as_next_session(self) -> None:
        self.rec.tick()
        proc = self.spawned[0][1]
        self.grow("XR18-0001.wav", 100)  # session 1 is on the drive
        self.listing = HEADER  # unplugged
        self.now += 1
        self.rec.tick()
        self.assertTrue(proc.killed)
        self.assertIsNone(self.rec.session)
        self.listing = XR18  # back
        self.rec.tick()
        self.assertEqual(self.out_of(1), "XR18-0002.wav")

    def test_stall_rebinds_after_stall_seconds(self) -> None:
        self.rec.tick()
        proc = self.spawned[0][1]
        self.grow("XR18-0001.wav", 100)
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
        self.grow("XR18-0001.wav", 1000)
        self.now = 1
        self.rec.tick()
        self.grow("XR18-0001-01.wav", 10)  # the next chunk starts smaller than the last one
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

    def test_status_record_while_recording_and_waiting(self) -> None:
        with self.assertLogs("recall", "INFO") as logs:
            self.rec.tick()
        self.assertIn("state=recording", logs.output[-1])
        self.assertIn("channels=18 rate=48000 bits=24", logs.output[-1])
        self.listing = HEADER  # no device
        with self.assertLogs("recall", "INFO") as logs:
            self.rec.tick()
        self.assertIn("state=waiting session_s=0 chunk_s=0", logs.output[-1])
        self.assertNotIn("device=", logs.output[-1])

    def test_status_counts_current_file_and_session_bytes(self) -> None:
        self.rec.tick()
        self.grow("XR18-0001.wav", 1000)
        self.now = 1
        with self.assertLogs("recall", "INFO") as logs:
            self.rec.tick()
        self.assertIn("file=XR18-0001.wav file_bytes=1000 session_bytes=1000", logs.output[-1])
        self.grow("XR18-0001-01.wav", 300)  # rollover: the first chunk is now closed at 1000 bytes
        self.now = 2
        with self.assertLogs("recall", "INFO") as logs:
            self.rec.tick()
        self.assertIn("file=XR18-0001-01.wav file_bytes=300 session_bytes=1300", logs.output[-1])

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
