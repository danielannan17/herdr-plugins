"""Persistent selected mode (global) and the running popup's pid, in one JSON file."""
import json
import os
import tempfile

from .config import MODES

DEFAULT_MODE = "work"


class State:
    def __init__(self, directory):
        self.path = os.path.join(directory, "state.json")

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data):
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def mode(self):
        mode = self._read().get("mode")
        return mode if mode in MODES else DEFAULT_MODE

    def set_mode(self, mode):
        data = self._read()
        data["mode"] = mode
        self._write(data)

    def popup(self):
        """(pid, tab_id) of the running popup, or (None, None)."""
        record = self._read().get("popup")
        if isinstance(record, dict) and isinstance(record.get("pid"), int):
            return record["pid"], record.get("tab_id")
        return None, None

    def set_popup(self, pid, tab_id=None):
        data = self._read()
        if pid is None:
            data.pop("popup", None)
        else:
            data["popup"] = {"pid": pid, "tab_id": tab_id}
        self._write(data)
