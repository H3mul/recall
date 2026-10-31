import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recall import __version__, cli, config, storage

MINIMAL = """
[device]
match = ["XR18"]
[storage]
uuid = "abcd-1234"
mount = "/mnt/rec"
dir = "recordings"
"""


def write(d, text, name="config.toml"):
    p = Path(d) / name
    p.write_text(text)
    return p


def run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.dict(os.environ, env or {}):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class Defaults(unittest.TestCase):
    def test_omitted_fields_yield_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.load(write(d, MINIMAL))
        self.assertEqual(cfg.match, ("XR18",))
        self.assertIsNone(cfg.max_channels)
        self.assertIsNone(cfg.max_rate)
        self.assertEqual((cfg.bit_depth, cfg.chunk_seconds, cfg.stall_seconds), (24, 900, 10))
        self.assertFalse(cfg.display)
        self.assertEqual(cfg.record_dir, "/mnt/rec/recordings")


class Validation(unittest.TestCase):
    def errors(self, text):
        with self.assertRaises(config.ConfigError) as cm:
            config.parse(__import__("tomllib").loads(text))
        return cm.exception.errors

    def test_multiple_errors_reported_together(self):
        errs = self.errors(MINIMAL.replace('["XR18"]', "[]") + "[capture]\nbit_depth = 20\n")
        self.assertEqual(len(errs), 2)

    def test_invalid_regex(self):
        self.assertIn("device.match", self.errors(MINIMAL.replace('"XR18"', '"("'))[0])

    def test_unknown_key(self):
        errs = self.errors(MINIMAL.replace('match = ["XR18"]', 'match = ["XR18"]\nmax_chanels = 4'))
        self.assertEqual(errs, ["device.max_chanels: unknown key"])

    def test_unknown_table(self):
        self.assertEqual(self.errors(MINIMAL + "[bogus]\n"), ["bogus: unknown key"])

    def test_non_positive_values(self):
        for key in ("chunk_seconds", "stall_seconds"):
            self.assertEqual(len(self.errors(MINIMAL + f"[capture]\n{key} = 0\n")), 1, key)
        for key in ("max_channels", "max_rate"):
            text = MINIMAL.replace('match = ["XR18"]', f'match = ["XR18"]\n{key} = -1')
            self.assertEqual(len(self.errors(text)), 1, key)

    def test_display_must_be_bool(self):
        self.assertEqual(len(self.errors('display = "yes"\n' + MINIMAL)), 1)

    def test_missing_required(self):
        self.assertEqual(len(self.errors("")), 4)


class Lookup(unittest.TestCase):
    def test_explicit_path_beats_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(d, MINIMAL)
            code, out, _ = run(["-c", str(p), "--check"], {"XDG_CONFIG_HOME": "/nonexistent"})
        self.assertEqual(code, 0)
        self.assertIn("device.match = ['XR18']", out)

    def test_xdg_path(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "recall").mkdir()
            write(Path(d) / "recall", MINIMAL)
            self.assertEqual(run(["--check"], {"XDG_CONFIG_HOME": d})[0], 0)

    def test_default_path(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".config" / "recall").mkdir(parents=True)
            write(Path(d) / ".config" / "recall", MINIMAL)
            env = {"HOME": d}
            with mock.patch.dict(os.environ, env):
                os.environ.pop("XDG_CONFIG_HOME", None)
                self.assertEqual(config.default_path(), Path(d) / ".config" / "recall" / "config.toml")
                self.assertEqual(cli.main(["--check"]), 0)

    def test_missing_file_is_config_error(self):
        code, _, err = run(["-c", "/nonexistent/config.toml", "--check"])
        self.assertEqual(code, 78)
        self.assertIn("/nonexistent/config.toml", err)


class Cli(unittest.TestCase):
    def test_version(self):
        code, out, _ = run(["--version"])
        self.assertEqual((code, out.strip()), (0, f"recall {__version__}"))

    def test_invalid_config_exits_78(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run(["-c", str(write(d, "[bogus]\n")), "--check"])[0], 78)

    def test_drive_missing_exits_69(self):
        with tempfile.TemporaryDirectory() as d:
            text = MINIMAL.replace("/mnt/rec", d)  # exists, but is not a mountpoint
            self.assertEqual(run(["-c", str(write(d, text))])[0], 69)

    def test_verbose_flag_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run(["-v", "-c", str(write(d, MINIMAL)), "--check"])[0], 0)


class Storage(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        root = Path(self.d.name)
        (root / "mnt" / "rec" / "recordings").mkdir(parents=True)
        (root / "by-uuid").mkdir()
        (root / "sdb1").touch()
        (root / "by-uuid" / "abcd-1234").symlink_to(root / "sdb1")
        self.cfg = config.Config(("XR18",), "abcd-1234", str(root / "mnt" / "rec"), "recordings")
        self.by_uuid = str(root / "by-uuid")
        self.root = root

    def info(self, fstype="ext4", source=None, mount=None):
        source = source or str(self.root / "sdb1")
        mount = mount or self.cfg.mount
        p = self.root / "mountinfo"
        p.write_text(f"36 1 8:17 / {mount} rw - {fstype} {source} rw\n")
        return str(p)

    def verify(self, **kw):
        storage.verify(self.cfg, self.info(**kw), self.by_uuid)

    def test_ok(self):
        self.verify()

    def test_not_a_mountpoint(self):
        with self.assertRaisesRegex(storage.StorageError, "not a mountpoint"):
            self.verify(mount="/elsewhere")

    def test_wrong_fs(self):
        with self.assertRaisesRegex(storage.StorageError, "ext4"):
            self.verify(fstype="vfat")

    def test_wrong_uuid(self):
        with self.assertRaisesRegex(storage.StorageError, "UUID"):
            self.verify(source=str(self.root / "other"))

    def test_missing_directory(self):
        self.cfg = config.Config(("XR18",), "abcd-1234", self.cfg.mount, "nope")
        with self.assertRaisesRegex(storage.StorageError, "does not exist"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
