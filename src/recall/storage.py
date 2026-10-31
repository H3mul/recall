"""Recording drive verification."""

import os


class StorageError(Exception):
    """The configured mountpoint is not the configured recording drive."""


def _unescape(field):
    # mountinfo escapes space, tab, newline and backslash as octal
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def mount_entry(mount, mountinfo="/proc/self/mountinfo"):
    """Return (fstype, source) if `mount` is a mountpoint, else None. Last mount wins."""
    found = None
    with open(mountinfo) as f:
        for line in f:
            left, _, right = line.partition(" - ")
            fields = left.split()
            if len(fields) > 4 and _unescape(fields[4]) == mount:
                fstype, source = right.split()[:2]
                found = (fstype, _unescape(source))
    return found


def verify(cfg, mountinfo="/proc/self/mountinfo", by_uuid="/dev/disk/by-uuid"):
    """Raise StorageError unless cfg.mount is an ext4 mountpoint with cfg.uuid and cfg.dir exists."""
    mount = os.path.normpath(cfg.mount)
    entry = mount_entry(mount, mountinfo)
    if entry is None:
        raise StorageError(f"{mount} is not a mountpoint: recordings must go to the external drive")
    fstype, source = entry
    if fstype != "ext4":
        raise StorageError(f"{mount} is {fstype}, expected ext4")
    try:
        expected = os.path.realpath(os.path.join(by_uuid, cfg.uuid))
    except OSError:
        expected = None
    if expected is None or not os.path.exists(os.path.join(by_uuid, cfg.uuid)):
        raise StorageError(f"no filesystem with UUID {cfg.uuid} is present")
    if os.path.realpath(source) != expected:
        raise StorageError(f"{mount} is not the drive with UUID {cfg.uuid}")
    if not os.path.isdir(cfg.record_dir):
        raise StorageError(f"recording directory {cfg.record_dir} does not exist")
