"""Configuration loading and validation. Stdlib only."""

import os
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BIT_DEPTHS = (16, 24, 32)

TOP_LEVEL = {"output_dir", "display"}
KNOWN: dict[str, set[str]] = {
    "device": {"match", "max_channels", "max_rate"},
    "capture": {"bit_depth", "chunk_seconds", "stall_seconds"},
}


class ConfigError(Exception):
    """Invalid configuration; `errors` names every invalid field."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Config:
    match: tuple[str, ...]
    output_dir: str
    max_channels: int | None = None
    max_rate: int | None = None
    bit_depth: int = 24
    chunk_seconds: int = 900
    stall_seconds: float = 10
    display: bool = False


def default_path(env: Mapping[str, str] = os.environ) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "recall" / "config.toml"


def load(path: str | os.PathLike[str]) -> Config:
    """Read, parse and validate `path`; raise ConfigError on any problem."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError([f"{path}: configuration file not found"]) from None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError([f"{path}: {e}"]) from None
    return parse(raw)


def _posint(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _abspath(v: Any) -> bool:
    return isinstance(v, str) and os.path.isabs(v)


def _posnum(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def parse(raw: Mapping[str, Any]) -> Config:
    errors: list[str] = []
    for key, val in raw.items():
        if key in TOP_LEVEL:
            continue
        if key not in KNOWN:
            errors.append(f"{key}: unknown key")
        elif not isinstance(val, dict):
            errors.append(f"{key}: must be a table")
        else:
            errors += [f"{key}.{k}: unknown key" for k in val if k not in KNOWN[key]]

    def table(name: str) -> Mapping[str, Any]:
        t = raw.get(name)
        return t if isinstance(t, dict) else {}

    dev, cap = table("device"), table("capture")

    def get(t: Mapping[str, Any], name: str, key: str, default: Any,
            check: Callable[[Any], bool], msg: str, required: bool = False) -> Any:
        path = f"{name}.{key}" if name else key
        if key not in t:
            if required:
                errors.append(f"{path}: required")
            return default
        if not check(t[key]):
            errors.append(f"{path}: {msg}")
            return default
        return t[key]

    match = get(
        dev, "device", "match", [],
        lambda v: isinstance(v, list) and (not v or all(isinstance(p, str) for p in v)),
        "must be an empty array or an array of strings", required=False,
    )
    for p in match:
        try:
            re.compile(p)
        except re.error as e:
            errors.append(f"device.match: invalid regular expression {p!r}: {e}")

    cfg = Config(
        match=tuple(match),
        output_dir=get(raw, "", "output_dir", "", _abspath, "must be an absolute path", True),
        max_channels=get(dev, "device", "max_channels", None, _posint, "must be a positive integer"),
        max_rate=get(dev, "device", "max_rate", None, _posint, "must be a positive integer"),
        bit_depth=get(
            cap, "capture", "bit_depth", 24,
            lambda v: v in BIT_DEPTHS and not isinstance(v, bool),
            f"must be one of {BIT_DEPTHS}",
        ),
        chunk_seconds=get(cap, "capture", "chunk_seconds", 900, _posint, "must be a positive integer"),
        stall_seconds=get(cap, "capture", "stall_seconds", 10, _posnum, "must be a positive number"),
        display=get(raw, "", "display", False, lambda v: isinstance(v, bool), "must be a boolean"),
    )
    if errors:
        raise ConfigError(errors)
    return cfg


def render(cfg: Config) -> str:
    """Resolved settings, one `key = value` line each, for --check."""
    rows = [
        ("device.match", list(cfg.match)),
        ("device.max_channels", cfg.max_channels),
        ("device.max_rate", cfg.max_rate),
        ("output_dir", cfg.output_dir),
        ("capture.bit_depth", cfg.bit_depth),
        ("capture.chunk_seconds", cfg.chunk_seconds),
        ("capture.stall_seconds", cfg.stall_seconds),
        ("display", cfg.display),
    ]
    return "\n".join(f"{k} = {v!r}" for k, v in rows)
