"""Command line entry point and exit statuses."""

import argparse
import logging
import sys

from recall import __version__, config, storage

EX_OK = 0
EX_FAILURE = 1
EX_UNAVAILABLE = 69  # recording drive missing or lost
EX_CONFIG = 78  # invalid configuration

log = logging.getLogger("recall")


def build_parser():
    p = argparse.ArgumentParser(prog="recall", description="Record every channel of a capture device.")
    p.add_argument("-c", "--config", metavar="PATH",
                   help="configuration file (default: $XDG_CONFIG_HOME/recall/config.toml, "
                        "else ~/.config/recall/config.toml)")
    p.add_argument("--check", action="store_true",
                   help="validate the configuration, print resolved settings and exit")
    p.add_argument("--probe", action="store_true",
                   help="bind and introspect the device, print the selection and exit")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="log at debug level")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"recall {__version__}")
        return EX_OK

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    path = args.config or config.default_path()
    try:
        cfg = config.load(path)
    except config.ConfigError as e:
        for err in e.errors:
            print(f"recall: configuration error: {err}", file=sys.stderr)
        return EX_CONFIG

    if args.check:
        print(config.render(cfg))
        return EX_OK

    if args.probe:
        log.error("--probe is not implemented yet")
        return EX_FAILURE

    try:
        storage.verify(cfg)
    except storage.StorageError as e:
        log.error("storage: %s", e)
        return EX_UNAVAILABLE

    log.error("recording is not implemented yet")
    return EX_FAILURE
