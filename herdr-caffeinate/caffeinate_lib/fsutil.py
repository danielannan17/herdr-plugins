"""Atomic JSON and text writes, and a tolerant JSON read."""

import json
import os
import tempfile


def write_atomic(path, text):
    """Write beside the target, then rename over it: a reader sees all or nothing."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, value):
    write_atomic(path, json.dumps(value, indent=1, sort_keys=True) + "\n")


def read_json(path, default=None):
    """The file's JSON, or `default` when it is missing or not (yet) valid.

    The Claude Code mod writes these files without a rename, so a reader can
    catch one half-written; treating that as absent for one pass is correct.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return default
