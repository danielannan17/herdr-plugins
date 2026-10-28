"""The popup's process: runs a dashboard in a pty and switches it without closing the popup."""
import fcntl
import os
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty

from . import herdr_api, screen
from .config import Config, ConfigError, MODES, herdr_prefix_binding, load
from .keys import Action, Router, leader_byte
from .resolve import message, resolve
from .state import State

QUICK_EXIT_SECONDS = 1.0
FOOTER_DELAY = 0.2
TAIL_BYTES = 8192


class Child:
    def __init__(self, proc, fd, started):
        self.proc, self.fd, self.started = proc, fd, started
        self.tail = b""


class Supervisor:
    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)
        self.config_dir = self.env["HERDR_PLUGIN_CONFIG_DIR"]
        self.config_path = os.path.join(self.config_dir, "config.toml")
        self.state = State(self.env["HERDR_PLUGIN_STATE_DIR"])
        self.mode = self.state.mode()
        self.workspace = herdr_api.workspace_from_context(herdr_api.context())
        self.home_tab = (self.workspace or {}).get("tab")
        self.config = Config()
        self.child = None
        self.plan = None
        self.router = Router()
        self.events = None
        self.quit = False
        self.footer_due = None
        self.ws_check_at = None
        self.rows, self.cols = 24, 80

    # ----- lifecycle -------------------------------------------------------

    def run(self):
        self.old_tty = termios.tcgetattr(0)
        self.sig_r, self.sig_w = os.pipe()
        os.set_blocking(self.sig_w, False)
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGWINCH, signal.SIGCHLD):
            signal.signal(sig, lambda s, _f: self._wake(s))
        tty.setraw(0)
        self.state.set_popup(os.getpid(), (self.workspace or {}).get('tab'))
        try:
            self.measure()
            try:
                self.events = herdr_api.subscribe_workspaces()
            except herdr_api.ApiError:
                self.events = None
            self.start(use_context=True)
            self.loop()
        finally:
            self.stop_child()
            self.out(screen.RESET + screen.CLEAR)
            self.state.set_popup(None)
            termios.tcsetattr(0, termios.TCSADRAIN, self.old_tty)
        return 0

    def _wake(self, signum):
        try:
            os.write(self.sig_w, bytes([signum]))
        except OSError:
            pass

    def loop(self):
        while not self.quit:
            fds = [0, self.sig_r]
            if self.child:
                fds.append(self.child.fd)
            if self.events:
                fds.append(self.events.fileno())
            try:
                ready, _, _ = select.select(fds, [], [], self.timeout())
            except InterruptedError:
                continue
            now = time.monotonic()
            for fd in ready:
                if fd == self.sig_r:
                    self.on_signals()
                elif fd == 0:
                    self.on_input(now)
                elif self.child and fd == self.child.fd:
                    self.on_child_output(now)
                elif self.events and fd == self.events.fileno():
                    self.on_event(now)
            self.on_timers(time.monotonic())

    def timeout(self):
        due = [t for t in (self.router.next_deadline(), self.footer_due, self.ws_check_at) if t is not None]
        wait = min(due) - time.monotonic() if due else 1.0
        return max(min(wait, 1.0), 0.0)

    # ----- terminal --------------------------------------------------------

    def out(self, text):
        data = text.encode() if isinstance(text, str) else text
        while data:
            try:
                data = data[os.write(1, data):]
            except BlockingIOError:
                select.select([], [1], [], 0.05)
            except OSError:
                return

    def measure(self):
        try:
            rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
        except OSError:
            rows = cols = 0
        self.rows, self.cols = rows or 24, cols or 80

    def child_rows(self):
        return self.rows - 1 if self.config.footer and self.rows >= 6 else self.rows

    def set_child_size(self):
        if self.child:
            size = struct.pack("HHHH", self.child_rows(), self.cols, 0, 0)
            fcntl.ioctl(self.child.fd, termios.TIOCSWINSZ, size)

    # ----- events ----------------------------------------------------------

    def on_signals(self):
        for sig in os.read(self.sig_r, 64):
            self.debug(f"signal {sig}")
            if sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
                self.quit = True
            elif sig == signal.SIGWINCH:
                self.measure()
                self.set_child_size()
                if self.child:
                    self.draw_footer()
                elif self.plan:
                    self.show_message(self.plan)
            elif sig == signal.SIGCHLD and self.child and self.child.proc.poll() is not None:
                self.child_exited()

    def on_input(self, now):
        data = os.read(0, 65536)
        if not data:
            self.quit = True
            return
        self.apply(self.router.feed(data, now))

    def on_child_output(self, now):
        try:
            data = os.read(self.child.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self.child.proc.wait()
            self.child_exited()
            return
        self.child.tail = (self.child.tail + data)[-TAIL_BYTES:]
        self.out(data)
        if self.footer_due is None:
            self.footer_due = now + FOOTER_DELAY

    def on_event(self, now):
        try:
            data = self.events.recv(65536)
        except OSError:
            data = b""
        if not data:
            self.events.close()
            self.events = None
            return
        self.ws_check_at = now + 0.2

    def on_timers(self, now):
        self.apply(self.router.poll(now))
        if self.footer_due is not None and now >= self.footer_due:
            self.footer_due = None
            self.draw_footer()
        if self.ws_check_at is not None and now >= self.ws_check_at:
            self.ws_check_at = None
            self.check_workspace()

    def apply(self, actions):
        for action in actions:
            if self.quit:
                return
            if action.kind == "forward":
                self.forward(action.data)
            elif action.kind == "close":
                self.quit = True
            elif action.kind == "retry":
                self.switch(self.mode)
            else:
                index = MODES.index(self.mode)
                target = {"next": MODES[(index + 1) % len(MODES)], "prev": MODES[(index - 1) % len(MODES)]}
                self.switch(target.get(action.kind, action.kind))

    def forward(self, data):
        if not self.child:
            return
        try:
            os.write(self.child.fd, data)
        except (BlockingIOError, OSError):
            pass

    # ----- dashboards ------------------------------------------------------

    def load_config(self):
        try:
            self.config = load(self.config_path)
        except ConfigError as e:
            self.config = e
        return self.config

    def key_setup(self):
        prefix, bound = herdr_prefix_binding(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            self.config_dir))), "config.toml"))
        cfg = self.config if isinstance(self.config, Config) else Config()
        try:
            self.leader = leader_byte(cfg.leader or prefix or "ctrl+b")
        except ValueError:
            self.leader = b"\x02"
        self.toggle = cfg.toggle or bound or "d"

    def start(self, use_context=False):
        """Resolve the current mode and show its dashboard or its message."""
        config = self.load_config()
        if self.mode == "work" and not use_context:
            self.workspace = herdr_api.current_workspace() or self.workspace
        self.plan = resolve(self.mode, config, self.workspace, self.env, self.config_path)
        self.debug(f"start {self.mode} ws={self.workspace} plan={self.plan.kind} {self.plan.lines or self.plan.launch.argv}")
        if self.plan.kind == "run":
            error = self.spawn(self.plan.launch)
            if error:
                self.plan = message(self.mode, ["Could not start the dashboard.", error,
                                                    "Command: " + " ".join(self.plan.launch.argv)])
                self.show_message(self.plan)
        else:
            self.show_message(self.plan)

    def debug(self, text):
        directory = self.env["HERDR_PLUGIN_STATE_DIR"]
        if os.path.exists(os.path.join(directory, "debug")):
            with open(os.path.join(directory, "debug.log"), "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {text}\n")

    def switch(self, mode):
        self.debug(f"switch {self.mode} -> {mode}")
        self.stop_child()
        self.mode = mode if mode in MODES else self.mode
        self.state.set_mode(self.mode)
        self.start()

    def check_workspace(self):
        """Herdr hides a popup whenever another tab is focused, so a dashboard there is closed rather
        than left running unseen. On the same tab, Work follows renames and restarts only when
        what would run changes."""
        current = herdr_api.current_workspace()
        if not current:
            return
        if self.home_tab and current.get("tab") != self.home_tab:
            self.debug(f"focus left {self.home_tab} for {current.get('tab')}: closing")
            self.quit = True
            return
        self.workspace = current
        if self.mode != "work":
            return
        plan = resolve(self.mode, self.load_config(), self.workspace, self.env, self.config_path)
        if self.plan is None or plan.signature() != self.plan.signature():
            self.stop_child()
            self.start()

    def bare_keys(self, launch):
        if launch is not None and launch.bare_keys is not None:
            return launch.bare_keys
        return self.config.bare_keys if isinstance(self.config, Config) else False

    def spawn(self, launch):
        self.stop_child()
        self.key_setup()
        self.router = Router(self.leader, self.toggle, bare=self.bare_keys(launch))
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", self.child_rows(), self.cols, 0, 0))
        try:
            proc = subprocess.Popen(
                list(launch.argv), stdin=slave, stdout=slave, stderr=slave, cwd=launch.cwd, env=launch.env,
                start_new_session=True, close_fds=True,
                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        except (OSError, subprocess.SubprocessError) as e:
            os.close(master)
            return str(e)
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.child = Child(proc, master, time.monotonic())
        self.out(screen.RESET + screen.CLEAR)
        self.footer_due = time.monotonic() + FOOTER_DELAY
        return None

    def child_exited(self):
        child, self.child = self.child, None
        try:
            while True:  # whatever the child printed before exiting
                data = os.read(child.fd, 65536)
                if not data:
                    break
                child.tail = (child.tail + data)[-TAIL_BYTES:]
                self.out(data)
        except OSError:
            pass
        os.close(child.fd)
        code = child.proc.returncode
        self.debug(f"child exited {code}")
        if code == 0 and time.monotonic() - child.started >= QUICK_EXIT_SECONDS:
            self.quit = True
            return
        what = f"Exited with status {code}." if code >= 0 else f"Killed by signal {-code}."
        if time.monotonic() - child.started < QUICK_EXIT_SECONDS:
            what = "Exited immediately. " + what
        lines = ["The dashboard stopped.", what, "Command: " + " ".join(child.proc.args)]
        output = screen.excerpt(child.tail)
        if output:
            lines += ["", "Last output:"] + output
        self.plan = message(self.mode, lines)
        self.show_message(self.plan)

    def stop_child(self):
        child, self.child = self.child, None
        if not child:
            return
        for sig, grace in ((signal.SIGHUP, 0.5), (signal.SIGTERM, 0.5), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(child.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and child.proc.poll() is None:
                time.sleep(0.01)
            if child.proc.poll() is not None:
                break
        try:
            os.killpg(child.proc.pid, signal.SIGKILL)  # stragglers left in the group
        except (ProcessLookupError, PermissionError):
            pass
        child.proc.wait()
        os.close(child.fd)
        self.footer_due = None

    # ----- drawing ---------------------------------------------------------

    def other_mode(self):
        return MODES[(MODES.index(self.mode) + 1) % len(MODES)]

    def key_label(self, *keys):
        name = "^" + chr(self.leader[0] + 64)
        return f"{name} {' '.join(keys)}"

    def show_message(self, plan):
        self.key_setup()
        self.router = Router(self.leader, self.toggle, message=True)
        other = screen.TITLES[self.other_mode()].split()[0]
        hints = [("Tab", f"Switch to {other}"), ("Esc", "Close")]
        if plan.reload_hint:
            hints.insert(1, ("r", "Reload config and retry"))
        self.out(screen.RESET + screen.render(plan.title, list(plan.lines), hints, self.rows, self.cols))

    def draw_footer(self):
        if not self.child or not self.config.footer or self.rows < 6:
            return
        label = (self.workspace or {}).get("label") or "-"
        title = screen.TITLES[self.mode].split()[0]
        left = f" {title} · {label}" if self.mode == "work" else f" {title}"
        other = screen.TITLES[self.other_mode()].split()[0]
        right = f"{self.key_label('Tab')} {other}  {self.key_label(self.toggle)} close "
        gap = self.cols - len(left) - len(right)
        text = (left + " " * gap + right) if gap > 0 else left[:self.cols - 1]
        self.out(f"\x1b7\x1b[{self.rows};1H\x1b[0;7m{text[:self.cols - 1]}\x1b[K\x1b[0m\x1b8")


def main():
    if not sys.stdin.isatty():
        sys.exit("herdr-dashboard run must be started by Herdr (it needs a terminal)")
    return Supervisor().run()
