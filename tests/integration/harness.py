"""Isolated Herdr server, pty helpers and a tiny terminal screen emulator.

Nothing here touches the user's running Herdr: every process runs with a
scrubbed environment whose config, state and sockets live in a temp dir.
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
from typing import Dict, List, Optional

HERDR = shutil.which("herdr") or "/opt/homebrew/bin/herdr"
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN_ID = "danielannan17.new-tab"


class Screen:
    """Minimal VT100/xterm emulator: enough for ratatui and this plugin's output."""

    def __init__(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.r = self.c = 0
        self._buf = ""

    def resize(self, rows: int, cols: int) -> None:
        self.__init__(rows, cols)

    def feed(self, data: bytes) -> None:
        self._buf += data.decode("utf-8", "replace")
        s, i, n = self._buf, 0, len(self._buf)
        while i < n:
            ch = s[i]
            if ch == "\x1b":
                if i + 1 >= n:
                    break
                nxt = s[i + 1]
                if nxt == "[":
                    j = i + 2
                    while j < n and not ("@" <= s[j] <= "~"):
                        j += 1
                    if j >= n:
                        break
                    self._csi(s[i + 2:j], s[j])
                    i = j + 1
                elif nxt in "]P_^X":  # string sequences end with BEL or ST
                    m = re.compile(r"\x07|\x1b\\").search(s, i + 2)
                    if not m:
                        break
                    i = m.end()
                else:
                    i += 2 if nxt not in "()#" else 3
                continue
            if ch == "\r":
                self.c = 0
            elif ch == "\n":
                self.r = min(self.rows - 1, self.r + 1)
            elif ch == "\b":
                self.c = max(0, self.c - 1)
            elif ch >= " ":
                if self.c >= self.cols:
                    self.c = 0
                    self.r = min(self.rows - 1, self.r + 1)
                self.grid[self.r][self.c] = ch
                self.c += 1
            i += 1
        self._buf = s[i:]

    def _csi(self, params: str, final: str) -> None:
        priv = params[:1] in "?><="
        nums = [int(p) if p.isdigit() else 0 for p in re.split(r"[;:]", params.lstrip("?><=")) if p != ""] or []
        def arg(k, d=1):
            return nums[k] if k < len(nums) and nums[k] else d
        if priv:
            return
        if final in "Hf":
            self.r = min(self.rows - 1, arg(0) - 1)
            self.c = min(self.cols - 1, arg(1) - 1)
        elif final == "A":
            self.r = max(0, self.r - arg(0))
        elif final == "B":
            self.r = min(self.rows - 1, self.r + arg(0))
        elif final == "C":
            self.c = min(self.cols - 1, self.c + arg(0))
        elif final == "D":
            self.c = max(0, self.c - arg(0))
        elif final == "G":
            self.c = min(self.cols - 1, arg(0) - 1)
        elif final == "d":
            self.r = min(self.rows - 1, arg(0) - 1)
        elif final == "K":
            mode = nums[0] if nums else 0
            lo, hi = {0: (self.c, self.cols), 1: (0, self.c + 1), 2: (0, self.cols)}.get(mode, (0, 0))
            for x in range(lo, hi):
                self.grid[self.r][x] = " "
        elif final == "J":
            mode = nums[0] if nums else 0
            if mode in (2, 3):
                self.grid = [[" "] * self.cols for _ in range(self.rows)]
            elif mode == 0:
                for x in range(self.c, self.cols):
                    self.grid[self.r][x] = " "
                for y in range(self.r + 1, self.rows):
                    self.grid[y] = [" "] * self.cols
        elif final == "X":
            for x in range(self.c, min(self.cols, self.c + arg(0))):
                self.grid[self.r][x] = " "
        elif final == "b" and self.c > 0:
            last = self.grid[self.r][self.c - 1]
            for _ in range(arg(0)):
                self.feed(last.encode())

    def text(self) -> str:
        return "\n".join("".join(row).rstrip() for row in self.grid)


class Pty:
    """A child process on a pty with an attached Screen.

    A reader thread drains the pty continuously: a child that cannot write its
    output (Herdr's client repaints a lot) stops reading input as well.
    """

    def __init__(self, argv: List[str], env: Dict[str, str], rows: int = 40, cols: int = 120, cwd: Optional[str] = None) -> None:
        self.screen = Screen(rows, cols)
        self.raw = b""
        pid, fd = pty.fork()
        if pid == 0:
            if cwd:
                os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.exit_code: Optional[int] = None
        self._lock = threading.Lock()
        self._stop = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while not self._stop:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.05)
                if not r:
                    continue
                data = os.read(self.fd, 65536)
            except (OSError, ValueError):
                return
            if not data:
                return
            with self._lock:
                self.raw += data
                self.screen.feed(data)

    def text(self) -> str:
        with self._lock:
            return self.screen.text()

    def pump(self, seconds: float = 0.2) -> None:
        time.sleep(seconds)

    def send(self, data) -> None:
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def wait_for(self, needle: str, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.text():
                return True
            time.sleep(0.02)
        return False

    def wait_gone(self, needle: str, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if needle not in self.text():
                return True
            time.sleep(0.02)
        return False

    def poll_exit(self, timeout: float = 5.0) -> Optional[int]:
        end = time.time() + timeout
        while time.time() < end:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.exit_code = os.waitstatus_to_exitcode(status)
                return self.exit_code
            time.sleep(0.01)
        return None

    def close(self) -> None:
        self._stop = True
        # Close the master before reaping: a child blocked draining output to a
        # pty nobody reads cannot exit until the master is gone.
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        for _ in range(100):
            try:
                pid, _status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return
            if pid:
                return
            time.sleep(0.05)


class IsolatedHerdr:
    """A private Herdr server (own config dir and sockets)."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="hnt.")
        for d in ("xdg", "home", "run"):
            os.makedirs(os.path.join(self.root, d))
        self.config_path = os.path.join(self.root, "xdg", "herdr", "config.toml")
        self.env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "HOME": os.path.join(self.root, "home"),
            "XDG_CONFIG_HOME": os.path.join(self.root, "xdg"),
            "XDG_STATE_HOME": os.path.join(self.root, "home", ".state"),
            "XDG_DATA_HOME": os.path.join(self.root, "home", ".data"),
            "XDG_CACHE_HOME": os.path.join(self.root, "home", ".cache"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "run"),
            "TMPDIR": os.path.join(self.root, "run") + "/",
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
        }
        self.socket_path = os.path.join(self.root, "xdg", "herdr", "herdr.sock")
        self.server: Optional[subprocess.Popen] = None

    def write_config(self, text: str) -> None:
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:
            f.write(text)

    def start(self) -> None:
        self.server = subprocess.Popen(
            [HERDR, "server"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        for _ in range(100):
            try:
                self.api("workspace", "list")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("isolated herdr server did not start")

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run([HERDR, *args], env=self.env, capture_output=True, text=True, timeout=20)
        if check and proc.returncode != 0:
            raise RuntimeError(f"herdr {' '.join(args)} failed: {proc.stdout}{proc.stderr}")
        return proc

    def api(self, *args: str) -> dict:
        proc = self.run(*args)
        return json.loads(proc.stdout)["result"]

    def link_plugin(self) -> None:
        self.run("plugin", "link", PLUGIN_ROOT)

    def workspaces(self) -> List[dict]:
        return self.api("workspace", "list")["workspaces"]

    def tabs(self, workspace_id: Optional[str] = None) -> List[dict]:
        args = ["tab", "list"] + (["--workspace", workspace_id] if workspace_id else [])
        return self.api(*args)["tabs"]

    def make_workspace(self, label: str, focus: bool = False) -> str:
        res = self.api("workspace", "create", "--label", label, "--cwd", self.root, "--focus" if focus else "--no-focus")
        return res["workspace"]["workspace_id"]

    def focused_workspace(self) -> Optional[str]:
        return next((w["workspace_id"] for w in self.workspaces() if w["focused"]), None)

    def stop(self) -> None:
        try:
            self.run("server", "stop", check=False)
        except Exception:
            pass
        if self.server:
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()
        # Any leftover process still running from this root's environment.
        subprocess.run(["pkill", "-f", self.root], capture_output=True)
        shutil.rmtree(self.root, ignore_errors=True)
