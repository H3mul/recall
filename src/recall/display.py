"""Status display: a 128x32 SSD1306 on I2C, drawn with luma.oled.

Install the optional dependency with `pip install recall[display]`. It is imported only when the display
is enabled, so recording needs nothing extra.
"""

import logging
import time
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger("recall")

I2C_PORT = 1  # /dev/i2c-1
I2C_ADDRESS = 0x3C
WIDTH, HEIGHT = 128, 32
LINE_HEIGHT = 8  # pixels per text line: four lines fit in 32
FONT_FILE = Path(__file__).parent / "fonts" / "spleen-5x8.otb"  # Spleen 5x8 bitmap font, BSD-2 (see fonts/)
RETRY_SECONDS = 60  # no more than one init attempt per minute while the panel is absent


def format_duration(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def status_lines(status: Mapping[str, Any]) -> list[str]:
    """Four lines from one status record: state, recording time, free storage, time left."""
    state = str(status["state"])
    if "channels" in status:
        state += f" {status['channels']}ch {status['rate'] // 1000}k"

    remaining = status.get("remaining_s")
    if remaining is None:
        left = "--"
    else:
        left = f"{remaining // 3600}h{remaining // 60 % 60:02d}m"

    return [
        state,
        "rec " + format_duration(status["session_s"]),
        f"free {status['free_bytes'] / 1e9:.1f} GB",
        "left " + left,
    ]


def open_ssd1306() -> Any:
    """Connect to the panel. Raises if luma.oled is missing or the panel does not answer."""
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306

    serial = i2c(port=I2C_PORT, address=I2C_ADDRESS)
    return ssd1306(serial, width=WIDTH, height=HEIGHT)


class Display:
    """Shows status records. Never raises: a failure is logged and init is retried once a minute."""

    def __init__(self, connect: Callable[[], Any] = open_ssd1306,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.connect = connect
        self.clock = clock
        self.device: Any = None
        self.next_attempt = 0.0

    def show(self, status: Mapping[str, Any]) -> None:
        try:
            if self.device is None:
                if self.clock() < self.next_attempt:
                    return
                self.next_attempt = self.clock() + RETRY_SECONDS
                self.device = self.connect()
            self.draw(status_lines(status))
        except Exception as e:  # noqa: BLE001 - the display must never stop recording
            log.error("display: %s", e)
            self.device = None  # reconnect at the next attempt

    def draw(self, lines: list[str]) -> None:
        from luma.core.render import canvas
        from PIL import ImageFont

        font = ImageFont.truetype(str(FONT_FILE), LINE_HEIGHT)
        with canvas(self.device) as draw:
            for row, line in enumerate(lines):
                draw.text((0, row * LINE_HEIGHT), line, font=font, fill="white")

    def clear(self) -> None:
        """Blank the panel and switch it off, for a graceful shutdown."""
        if self.device is None:
            return
        try:
            self.device.clear()
            self.device.hide()
        except Exception as e:  # noqa: BLE001
            log.error("display: %s", e)
        self.device = None
