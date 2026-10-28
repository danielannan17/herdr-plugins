"""Runs the real popup process (dashboard.py run) on a pty, the way Herdr does."""
import fcntl
import json
import os
import pty
import select
import signal
import struct
import sys
import tempfile
import termios
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Popup:
    def __init__(self, config_text, workspace=None, state_dir=None, rows=30, cols=100, extra_env=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = os.path.join(self.tmp.name, "config", "plugins", "config", "herdr-dashboard")
        os.makedirs(self.config_dir)
        with open(os.path.join(self.config_dir, "config.toml"), "w") as f:
            f.write(config_text)
        self.state_dir = state_dir or os.path.join(self.tmp.name, "state")
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env.update(HERDR_PLUGIN_CONFIG_DIR=self.config_dir, HERDR_PLUGIN_STATE_DIR=self.state_dir,
                   HERDR_PLUGIN_ID="herdr-dashboard", TERM="xterm-256color")
        if workspace:
            env["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"workspace_id": "w1", "workspace_label": workspace,
                                                           "focused_pane_cwd": self.tmp.name})
        env.update(extra_env or {})
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(ROOT)
            os.execvpe(sys.executable, [sys.executable, "dashboard.py", "run"], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.buffer = b""
        self.mark = 0  # wait_for looks at output since the last input
        self.status = None

    def read(self, seconds=0.3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.buffer += data

    def wait_for(self, text, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if text.encode() in self.buffer[self.mark:]:
                return True
            self.read(0.1)
        return False

    def send(self, data):
        self.read(0.05)
        self.mark = len(self.buffer)
        os.write(self.fd, data if isinstance(data, bytes) else data.encode())

    def exited(self, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.status = status
                return True
            self.read(0.05)
        return False

    def close(self):
        if self.status is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except OSError:
                pass
        os.close(self.fd)
        self.tmp.cleanup()

    def mode(self):
        try:
            with open(os.path.join(self.state_dir, "state.json")) as f:
                return json.load(f).get("mode")
        except (OSError, ValueError):
            return None
