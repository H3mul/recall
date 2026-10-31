# recall

Record all: supervises SoX to capture every channel of an ALSA device (a Behringer XR18 by default) into gapless multitrack WAV chunks on an external ext4 drive.

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
| 69 | recording drive missing or lost |
| other | any other failure |

`--check` validates the file and prints the resolved settings. It does not look at the drive.

## Configuration

A commented template is in [`config.example.toml`](config.example.toml). Unknown keys are errors. Only `device.match` and the three `[storage]` keys are required.

```toml
[device]
# Regular expressions searched in each `arecord -l` line; list order wins, then lowest card number.
match = ["XR18", "X18"]
# max_channels = 18   # optional ceiling; default: every channel the device offers
# max_rate = 48000    # optional ceiling; default: the best rate the device offers

[storage]
uuid = "0a1b2c3d-1111-2222-3333-444455556666"  # ext4 filesystem UUID of the recording drive
mount = "/mnt/recordings"                      # must be a mountpoint of that filesystem
dir = "recall"                                 # recording directory below the mountpoint; must exist

[capture]
bit_depth = 24        # 16, 24 or 32
chunk_seconds = 900   # also capped so a chunk stays under 4 GB
stall_seconds = 10    # no output growth for this long means rebind

# display = false     # opt in to the SSD1306 status display
```
