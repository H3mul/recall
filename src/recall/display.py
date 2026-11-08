"""SSD1306 128x64 status display over /dev/i2c-1. Stdlib only; shows the telemetry status record.

Quick and dirty: whole frame redrawn each tick, four text lines, one page each.
"""

import fcntl
import logging
import os
import time
from collections.abc import Callable, Mapping

log = logging.getLogger("recall")

I2C_SLAVE = 0x0703
RETRY_S = 60.0  # no more than one init attempt per minute while the panel is absent

INIT = bytes([0xAE, 0xD5, 0x80, 0xA8, 0x1F, 0xD3, 0x00, 0x40, 0x8D, 0x14, 0x20, 0x00, 0xA1, 0xC8,
              0xDA, 0x02, 0x81, 0xCF, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6, 0xAF])
HOME = bytes([0x21, 0, 127, 0x22, 0, 3])  # column and page window: the whole 128x32 screen

# 5x7 font, ASCII 0x20..0x7E, five column bytes per glyph (bit 0 = top row)
_FONT = bytes.fromhex("""
0000000000 00005f0000 0007000700 147f147f14 242a7f2a12 2313086462 3649552250 0005030000
001c224100 0041221c00 14083e0814 08083e0808 0050300000 0808080808 0060600000 2010080402
3e5149453e 00427f4000 4261514946 2141454b31 1814127f10 2745454539 3c4a494930 0171090503
3649494936 0649492906 0036360000 0056360000 0814224100 1414141414 0041221408 0201510906
324979413e 7e1111117e 7f49494936 3e41414122 7f4141221c 7f49494941 7f09090901 3e4149497a
7f0808087f 00417f4100 2040413f01 7f08142241 7f40404040 7f020c027f 7f0408107f 3e4141413e
7f09090906 3e4151215e 7f09192946 4649494931 01017f0101 3f4040403f 1f2040201f 3f4038403f
6314081463 0708700807 6151494543 007f414100 0204081020 0041417f00 0402010204 4040404040
0001020400 2054545478 7f48444438 3844444420 384444487f 3854545418 087e090102 0c5252523e
7f08040478 00447d4000 2040443d00 7f10284400 00417f4000 7c04180478 7c08040478 3844444438
7c14141408 081414187c 7c08040408 4854545420 043f444020 3c4040207c 1c2040201c 3c4030403c
4428102844 0c5050503c 4464544c44 0008364100 00007f0000 0041360800 1008081008
""".replace("\n", " "))
assert len(_FONT) == 95 * 5


def fmt_time(s: int) -> str:
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def lines(f: Mapping[str, object]) -> list[str]:
    """Four 21-character lines from one status record: state, recording time, free storage, time left."""
    state = str(f["state"])
    if "channels" in f:
        state += f" {f['channels']}ch {int(f['rate']) // 1000}k"  # type: ignore[call-overload]
    left = f.get("remaining_s")
    left_s = "--" if left is None else f"{int(left) // 3600}h{int(left) // 60 % 60:02d}m"  # type: ignore[call-overload]
    return [state, "rec " + fmt_time(int(f["session_s"])),  # type: ignore[call-overload]
            f"free {int(f['free_bytes']) / 1e9:.1f} GB",  # type: ignore[call-overload]
            "left " + left_s]


def frame(text: list[str]) -> bytes:
    """128x4 page bytes for the whole screen: one 5x7 text line per page, 21 chars by 4 lines."""
    pages = [bytearray(128) for _ in range(4)]
    for row, line in enumerate(text[:4]):
        for i, ch in enumerate(line[:21]):
            g = (ord(ch) - 32) if 32 <= ord(ch) <= 126 else 31  # unknown -> '?'
            pages[row][i * 6:i * 6 + 5] = _FONT[g * 5:g * 5 + 5]
    return b"".join(pages)


class Display:
    """Never raises: a failure is logged, the panel is dropped, and init is retried once a minute."""

    def __init__(self, dev: str = "/dev/i2c-1", addr: int = 0x3C,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.dev, self.addr, self.clock = dev, addr, clock
        self.fd: int | None = None
        self.next_try = 0.0

    def _cmd(self, data: bytes) -> None:
        assert self.fd is not None
        os.write(self.fd, b"\x00" + data)  # 0x00 = control byte: command

    def _open(self) -> None:
        self.fd = os.open(self.dev, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.addr)
        for b in INIT:  # one command per transfer: always accepted
            self._cmd(bytes([b]))

    def show(self, fields: Mapping[str, object]) -> None:
        try:
            if self.fd is None:
                if self.clock() < self.next_try:
                    return
                self.next_try = self.clock() + RETRY_S
                self._open()
            for b in HOME:
                self._cmd(bytes([b]))
            img = frame(lines(fields))
            for p in range(4):  # one page per transfer, 0x40 = control byte: data
                os.write(self.fd, b"\x40" + img[p * 128:(p + 1) * 128])  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001 - the display must never stop recording
            log.error("display: %s", e)
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def clear(self) -> None:
        """Blank the panel and turn it off, for a graceful shutdown. Skipped if the panel never came up."""
        if self.fd is None:
            return
        try:
            for b in HOME:
                self._cmd(bytes([b]))
            for _ in range(4):
                os.write(self.fd, b"\x40" + bytes(128))
            self._cmd(b"\xAE")  # display off
        except Exception as e:  # noqa: BLE001
            log.error("display: %s", e)
        finally:
            os.close(self.fd)
            self.fd = None
