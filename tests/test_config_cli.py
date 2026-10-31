import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recall import __version__, cli, config, storage

MINIMAL = """
output_dir = "/mnt/rec/recordings"
[device]
match = ["XR18"]
"""


def write(d: str | Path, text: str, name: str = "config.toml") -> Path:
    p = Path(d) / name
    p.write_text(text)
    return p


def run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.dict(os.environ, env or {}):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class Defaults(unittest.TestCase):
    def test_omitted_fields_yield_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = config.load(write(d, MINIMAL))
        self.assertEqual(cfg.match, ("XR18",))
        self.assertIsNone(cfg.max_channels)
        self.assertIsNone(cfg.max_rate)
        self.assertEqual((cfg.bit_depth, cfg.chunk_seconds, cfg.stall_seconds), (24, 900, 10))
        self.assertFalse(cfg.display)
        self.assertEqual(cfg.output_dir, "/mnt/rec/recordings")


class Example(unittest.TestCase):
    def test_example_file_is_valid(self) -> None:
        path = Path(__file__).parent.parent / "config.example.toml"
        self.assertEqual(config.load(path).match, ("XR18", "X18"))


class Validation(unittest.TestCase):
    def errors(self, text: str) -> list[str]:
        with self.assertRaises(config.ConfigError) as cm:
            config.parse(__import__("tomllib").loads(text))
        return cm.exception.errors

    def test_multiple_errors_reported_together(self) -> None:
        errs = self.errors(MINIMAL.replace('["XR18"]', '["("]') + "[capture]\nbit_depth = 20\n")
        self.assertEqual(len(errs), 2)

    def test_empty_match_list_is_valid(self) -> None:
        cfg = config.parse(__import__("tomllib").loads(MINIMAL.replace('["XR18"]', "[]")))
        self.assertEqual(cfg.match, ())

    def test_match_may_be_omitted(self) -> None:
        cfg = config.parse(__import__("tomllib").loads(MINIMAL.replace('match = ["XR18"]', "")))
        self.assertEqual(cfg.match, ())

    def test_match_must_be_strings(self) -> None:
        self.assertEqual(len(self.errors(MINIMAL.replace('["XR18"]', "[1]"))), 1)

    def test_invalid_regex(self) -> None:
        self.assertIn("device.match", self.errors(MINIMAL.replace('"XR18"', '"("'))[0])

    def test_unknown_key(self) -> None:
        errs = self.errors(MINIMAL.replace('match = ["XR18"]', 'match = ["XR18"]\nmax_chanels = 4'))
        self.assertEqual(errs, ["device.max_chanels: unknown key"])

    def test_unknown_table(self) -> None:
        self.assertEqual(self.errors(MINIMAL + "[bogus]\n"), ["bogus: unknown key"])

    def test_non_positive_values(self) -> None:
        for key in ("chunk_seconds", "stall_seconds"):
            self.assertEqual(len(self.errors(MINIMAL + f"[capture]\n{key} = 0\n")), 1, key)
        for key in ("max_channels", "max_rate"):
            text = MINIMAL.replace('match = ["XR18"]', f'match = ["XR18"]\n{key} = -1')
            self.assertEqual(len(self.errors(text)), 1, key)

    def test_display_must_be_bool(self) -> None:
        self.assertEqual(len(self.errors('display = "yes"\n' + MINIMAL)), 1)

    def test_missing_required(self) -> None:
        self.assertEqual(self.errors(""), ["output_dir: required"])

    def test_output_dir_must_be_absolute(self) -> None:
        self.assertEqual(self.errors('output_dir = "recordings"'), ["output_dir: must be an absolute path"])

    def test_storage_table_is_gone(self) -> None:
        errs = self.errors(MINIMAL + '[storage]\nuuid = "abcd-1234"\n')
        self.assertEqual(errs, ["storage: unknown key"])


class Lookup(unittest.TestCase):
    def test_explicit_path_beats_default(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = write(d, MINIMAL)
            code, out, _ = run(["-c", str(p), "--check"], {"XDG_CONFIG_HOME": "/nonexistent"})
        self.assertEqual(code, 0)
        self.assertIn("device.match = ['XR18']", out)

    def test_xdg_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "recall").mkdir()
            write(Path(d) / "recall", MINIMAL)
            self.assertEqual(run(["--check"], {"XDG_CONFIG_HOME": d})[0], 0)

    def test_default_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".config" / "recall").mkdir(parents=True)
            write(Path(d) / ".config" / "recall", MINIMAL)
            env = {"HOME": d}
            with mock.patch.dict(os.environ, env):
                os.environ.pop("XDG_CONFIG_HOME", None)
                self.assertEqual(config.default_path(), Path(d) / ".config" / "recall" / "config.toml")
                self.assertEqual(cli.main(["--check"]), 0)

    def test_missing_file_is_config_error(self) -> None:
        code, _, err = run(["-c", "/nonexistent/config.toml", "--check"])
        self.assertEqual(code, 78)
        self.assertIn("/nonexistent/config.toml", err)


class Cli(unittest.TestCase):
    def test_version(self) -> None:
        code, out, _ = run(["--version"])
        self.assertEqual((code, out.strip()), (0, f"recall {__version__}"))

    def test_invalid_config_exits_78(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run(["-c", str(write(d, "[bogus]\n")), "--check"])[0], 78)

    def test_drive_missing_exits_69(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            text = MINIMAL.replace("/mnt/rec/recordings", f"{d}/missing")
            code, _, _ = run(["-c", str(write(d, text))])
            self.assertEqual(code, 69)
            self.assertFalse(os.path.exists(f"{d}/missing"))  # never created

    def test_verbose_flag_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run(["-v", "-c", str(write(d, MINIMAL)), "--check"])[0], 0)


class Storage(unittest.TestCase):
    def test_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            storage.verify(d)

    def test_missing_directory_is_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "nope"
            with self.assertRaisesRegex(storage.StorageError, "does not exist"):
                storage.verify(str(missing))
            self.assertFalse(missing.exists())

    def test_directory_removed_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "rec"
            out.mkdir()
            storage.verify(str(out))
            out.rmdir()
            with self.assertRaises(storage.StorageError):
                storage.verify(str(out))

    def test_file_is_not_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "f"
            f.touch()
            with self.assertRaises(storage.StorageError):
                storage.verify(str(f))


if __name__ == "__main__":
    unittest.main()
