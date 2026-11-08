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
ICON_SIZE = 22  # three text lines high
ICON_X, ICON_Y = WIDTH - ICON_SIZE, 0  # top right corner
FONTS = Path(__file__).parent / "fonts"
FA = FONTS / "fa-solid-subset.otf"  # Font Awesome 6 Free (OFL): the hourglass, dot and triangle
EMOJI = FONTS / "noto-emoji-subset.ttf"  # Noto Emoji (OFL): the snail
# (font file, glyph); drawn as large as fits the icon box
HOURGLASS = (FA, "\uf252")
RECORD_DOT = (FA, "\uf111")
ALERT = (FA, "\uf071")
SNAIL = (EMOJI, "\U0001F40C")
GLYPH_WIDTH = 5
RETRY_SECONDS = 60  # no more than one init attempt per minute while the panel is absent


def format_duration(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def status_rows(status: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Two (label, value) rows under the status heading.

    Recording: time, left.
    Waiting: wanted device names, observed ALSA device names.
    """
    if status["state"] == "recording":
        remaining = status["remaining_s"]
        return [
            ("time", format_duration(status["session_s"])),
            ("left", f"{remaining // 3600}h{remaining // 60 % 60:02d}m"),
        ]

    return [
        ("want", ",".join(status.get("want", [])) or "any"),
        ("have", ",".join(status.get("have", [])) or "none"),
    ]


def status_footer(status: Mapping[str, Any]) -> tuple[str, str, str]:
    """The bottom line, always shown: free storage (left), write rate (center), IO stall (right).

    While not recording the rate and stall read 0. While recording, an IO stall the kernel cannot
    report (no PSI) shows as `--`, not 0.
    """
    recording = status["state"] == "recording"
    stall = status.get("io_stall_pct", None if recording else 0)
    return (f"hdd:{status['free_bytes'] / 1e9:.0f}G",
            f"{status.get('write_bps', 0) / 1e6:.1f}MB/s",
            f"io:{'--' if stall is None else f'{stall:.0f}%'}")


def pick_icon(status: Mapping[str, Any]) -> tuple[Path, str]:
    """Hourglass while waiting. While recording: alert after a recent overrun, snail if the drive is
    behind, otherwise the record dot."""
    if status["state"] != "recording":
        return HOURGLASS
    if status.get("overrun_recent"):
        return ALERT
    if status.get("behind"):
        return SNAIL
    return RECORD_DOT


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
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(str(FONT_FILE), LINE_HEIGHT)
        rows = status_rows(status)
        # Values line up just past the widest "label: " pad.
        value_x = int(max(font.getlength(label + ": ") for label, _ in rows))
        value_chars = (ICON_X - value_x) // GLYPH_WIDTH  # rows 1-2 end before the icon

        image = Image.new("1", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(image)
        for row, (label, value) in enumerate(rows, start=1):
            y = row * LINE_HEIGHT
            draw.text((0, y), label + ":", font=font, fill=1)
            draw.text((value_x, y), value[:value_chars], font=font, fill=1)

        # Bottom row: first item anchored left, second centered, third anchored right.
        y = 3 * LINE_HEIGHT
        left, center, right = status_footer(status)
        draw.text((0, y), left, font=font, fill=1)
        draw.text((int((WIDTH - font.getlength(center)) // 2), y), center, font=font, fill=1)
        draw.text((int(WIDTH - font.getlength(right)), y), right, font=font, fill=1)

        icon_file, glyph = pick_icon(status)
        size = ICON_SIZE
        while True:  # the largest font size whose glyph ink fits the icon box
            icon_font = ImageFont.truetype(str(icon_file), size)
            ink_left, ink_top, ink_right, ink_bottom = icon_font.getbbox(glyph)
            if size <= 1 or max(ink_right - ink_left, ink_bottom - ink_top) <= ICON_SIZE:
                break
            size -= 1
        draw.text((ICON_X + (ICON_SIZE - ink_left - ink_right) // 2, ICON_Y + (ICON_SIZE - ink_top - ink_bottom) // 2),
                  glyph, font=icon_font, fill=1)  # centered on the ink, not the advance box

        # The status heading: upper case, centered between the left edge and the icon.
        text = str(status["state"]).upper()
        draw.text((int((ICON_X - font.getlength(text)) // 2), 0), text, font=font, fill=1)
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
