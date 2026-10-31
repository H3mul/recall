"""Output directory check. `recall` never creates the directory or inspects its filesystem."""

import os


class StorageError(Exception):
    """The output directory does not exist."""


def verify(output_dir: str) -> None:
    """Raise StorageError unless `output_dir` exists. Also used to re-check while recording."""
    if not os.path.isdir(output_dir):
        raise StorageError(f"output directory {output_dir} does not exist")
