# recall

Record all: supervises arecord to capture every channel of an ALSA device (a Behringer XR18 by default) into gapless multitrack WAV chunks in a directory you choose.

Python 3.11+, standard library only.

## Usage

```
recall [-c PATH] [--check] [--probe] [--version] [-v]
```

The configuration file is `-c PATH`, else `$XDG_CONFIG_HOME/recall/config.toml`, else `~/.config/recall/config.toml`.

| Exit | Meaning |
|------|---------|
| 0 | clean stop |
| 78 | invalid configuration |
| 69 | output directory missing or lost |
| other | any other failure |

`--check` validates the file and prints the resolved settings. It does not look at the output directory.

## Configuration

A commented template is in [`config.example.toml`](config.example.toml). Unknown keys are errors. Only `output_dir` is required.

```toml
# Absolute path of the directory recordings go to. It must already exist: recall never creates it and does not
# inspect its filesystem. Choosing and mounting the target is up to you.
output_dir = "/mnt/recordings/recall"

# display = false     # opt in to the SSD1306 status display (top level, before any table)

[device]
# Regular expressions searched in each `arecord -l` line; list order wins, then lowest card number.
# Empty or omitted: the first capture device (lowest card number).
match = ["XR18", "X18"]
# max_channels = 18   # optional ceiling; default: every channel the device offers
# max_rate = 48000    # optional ceiling; default: the best rate the device offers

[capture]
bit_depth = 24        # 16, 24 or 32; the device's deepest format if it lacks this
chunk_seconds = 900   # also capped so a chunk stays under 4 GB
stall_seconds = 10    # no output growth for this long means rebind
```
