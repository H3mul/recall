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
ICON_SIZE = 16
ICON_X = WIDTH - ICON_SIZE  # right edge of the screen
ICON_Y = (HEIGHT - ICON_SIZE) // 2  # centered vertically
ICON_FILE = Path(__file__).parent / "fonts" / "fa-solid-subset.otf"  # Font Awesome 6 Free, OFL: two glyphs
HOURGLASS = ("\uf252", 16)  # (glyph, font size) chosen so each fills the 16 px box
RECORD_DOT = ("\uf111", 14)
GLYPH_WIDTH = 5
RETRY_SECONDS = 60  # no more than one init attempt per minute while the panel is absent


def format_duration(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def status_rows(status: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Three (label, value) rows under the status heading. The free-storage row is always last.

    Recording: time, left, free.
    Waiting: wanted device names, observed ALSA device names, free.
    """
    free = ("free", f"{status['free_bytes'] / 1e9:.1f} GB")

    if status["state"] == "recording":
        remaining = status["remaining_s"]
        return [
            ("time", format_duration(status["session_s"])),
            ("left", f"{remaining // 3600}h{remaining // 60 % 60:02d}m"),
            free,
        ]

    return [
        ("want", ",".join(status.get("want", [])) or "any"),
        ("have", ",".join(status.get("have", [])) or "none"),
        free,
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
            self.draw(status)
        except Exception as e:  # noqa: BLE001 - the display must never stop recording
            log.error("display: %s", e)
            self.device = None  # reconnect at the next attempt

    def draw(self, status: Mapping[str, Any]) -> None:
        from PIL import Image, ImageChops, ImageDraw, ImageFont

        font = ImageFont.truetype(str(FONT_FILE), LINE_HEIGHT)
        glyph, size = RECORD_DOT if status["state"] == "recording" else HOURGLASS
        icon_font = ImageFont.truetype(str(ICON_FILE), size)

        rows = status_rows(status)
        # Values line up just past the widest "label: " pad.
        value_x = int(max(font.getlength(label + ": ") for label, _ in rows))
        value_chars = (WIDTH - value_x) // GLYPH_WIDTH
        value_chars = min(value_chars, (ICON_X - value_x) // GLYPH_WIDTH)  # rows 1-3 share the icon's columns

        image = Image.new("1", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(image)
        for row, (label, value) in enumerate(rows, start=1):
            y = row * LINE_HEIGHT
            draw.text((0, y), label + ":", font=font, fill=1)
            draw.text((value_x, y), value[:value_chars], font=font, fill=1)
        draw.text((ICON_X + ICON_SIZE // 2, ICON_Y + ICON_SIZE // 2), glyph, font=icon_font, fill=1, anchor="mm")

        # The status heading: centered on the full width, bold on vertical strokes only.
        heading = Image.new("1", (WIDTH, HEIGHT))
        text = str(status["state"])
        ImageDraw.Draw(heading).text((int((WIDTH - font.getlength(text) - 1) // 2), 0), text, font=font, fill=1)
        vertical = ImageChops.logical_and(heading, ImageChops.logical_or(ImageChops.offset(heading, 0, 1),
                                                                         ImageChops.offset(heading, 0, -1)))
        image = ImageChops.logical_or(image, ImageChops.logical_or(heading, ImageChops.offset(vertical, 1, 0)))
        self.device.display(image)

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
