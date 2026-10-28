"""An isolated Herdr (own config dir, state dir, socket) hosted in tmux so keys can be sent to it.

The live Herdr session is never touched: HERDR_* variables are dropped and XDG_* point into a temp dir.
"""
import json
import os
import re
import subprocess
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOKENS = {"Tab": "Tab", "BTab": "BTab", "Esc": "Escape", "Enter": "Enter", "C-b": "C-b"}


class Herdr:
    def __init__(self, herdr_config="", plugin_config=""):
        self.base = tempfile.mkdtemp(prefix="hd-int-")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        self.env.update(XDG_CONFIG_HOME=self.base + "/xdg", XDG_STATE_HOME=self.base + "/state", TERM="xterm-256color")
        self.config_dir = self.base + "/xdg/herdr"
        self.socket = self.config_dir + "/herdr.sock"
        self.tmux = "hdint" + str(os.getpid())
        os.makedirs(self.config_dir)
        with open(self.config_dir + "/config.toml", "w") as f:
            f.write('onboarding = false\n[[keys.command]]\nkey = "prefix+d"\ntype = "plugin_action"\n'
                    'command = "herdr-dashboard.open"\n' + herdr_config)
        self.plugin_config_path = None
        self.plugin_config = plugin_config

    # ----- running things ---------------------------------------------------
    def run(self, *argv, check=False):
        out = subprocess.run(list(argv), env=self.env, capture_output=True, text=True, timeout=30)
        if check and out.returncode:
            raise RuntimeError(f"{argv}: {out.stderr or out.stdout}")
        return out

    def herdr(self, *args):
        """Parsed JSON of a herdr CLI call (stdout, or stderr when it failed)."""
        out = self.run("herdr", *args)
        text = out.stdout.strip() or out.stderr.strip()
        try:
            return json.loads(text)
        except ValueError:
            return {"raw": text}

    def start(self):
        self.herdr("plugin", "link", ROOT)
        self.herdr("plugin", "link", ROOT + "/tests/integration/overlay-plugin")
        self.plugin_config_path = self.run("herdr", "plugin", "config-dir", "herdr-dashboard").stdout.strip() + "/config.toml"
        self.write_plugin_config(self.plugin_config)
        os.makedirs(self.base + "/state/herdr/plugins/herdr-dashboard", exist_ok=True)
        open(self.base + "/state/herdr/plugins/herdr-dashboard/debug", "w").close()
        subprocess.run(["tmux", "-L", self.tmux, "new-session", "-d", "-s", "t", "-x", "200", "-y", "50",
                        "herdr; sleep 60"], env=self.env, check=True)
        for _ in range(50):
            if self.run("herdr", "status").stdout.count("status: running"):
                break
            time.sleep(0.2)
        time.sleep(0.5)

    def reset_mode(self):
        path = self.base + "/state/herdr/plugins/herdr-dashboard"
        os.makedirs(path, exist_ok=True)
        with open(path + "/state.json", "w") as f:
            f.write('{"mode": "work"}')

    def write_plugin_config(self, text):
        with open(self.plugin_config_path, "w") as f:
            f.write(text)

    def stop(self):
        self.run("herdr", "server", "stop")
        subprocess.run(["tmux", "-L", self.tmux, "kill-server"], capture_output=True)

    # ----- screen and keys ---------------------------------------------------
    def keys(self, *keys, delay=0.15):
        for k in keys:
            subprocess.run(["tmux", "-L", self.tmux, "send-keys", "-t", "t", TOKENS.get(k, k)], check=True)
            time.sleep(delay)

    def type(self, text):
        subprocess.run(["tmux", "-L", self.tmux, "send-keys", "-t", "t", "-l", text], check=True)

    def screen(self):
        return subprocess.run(["tmux", "-L", self.tmux, "capture-pane", "-t", "t", "-p"],
                              capture_output=True, text=True).stdout

    def resize(self, cols, rows):
        subprocess.run(["tmux", "-L", self.tmux, "resize-window", "-t", "t", "-x", str(cols), "-y", str(rows)], check=True)

    def wait_screen(self, text, timeout=8.0, absent=False):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if (text in self.screen()) != absent:
                return True
            time.sleep(0.15)
        return False

    # ----- herdr state ---------------------------------------------------------
    def snapshot(self):
        return self.herdr("api", "snapshot")["result"]["snapshot"]

    def workspaces(self):
        return {w["label"]: w["workspace_id"] for w in self.snapshot()["workspaces"]}

    def create_workspace(self, label, cwd):
        os.makedirs(cwd, exist_ok=True)
        r = self.herdr("workspace", "create", "--cwd", cwd, "--label", label, "--no-focus")
        return r["result"]["workspace"]["workspace_id"], r["result"]["root_pane"]["pane_id"]

    def focus(self, label_or_id):
        ws = self.workspaces().get(label_or_id, label_or_id)
        self.herdr("workspace", "focus", ws)
        time.sleep(0.3)

    def layout_signature(self):
        s = self.snapshot()
        return json.dumps({"layouts": s["layouts"], "ws": s["focused_workspace_id"], "tab": s["focused_tab_id"],
                           "pane": s["focused_pane_id"], "panes": sorted(p["pane_id"] for p in s["panes"])}, sort_keys=True)

    # ----- processes -------------------------------------------------------------
    @staticmethod
    def ps():
        rows = {}
        for line in subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,command="], capture_output=True, text=True).stdout.splitlines():
            m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)", line)
            if m:
                rows[int(m.group(1))] = (int(m.group(2)), int(m.group(3)), m.group(4))
        return rows

    def supervisors(self):
        return [pid for pid, (_, _, cmd) in self.ps().items() if re.search(r"dashboard\.py run$", cmd)
                and self._ours(pid)]

    def _ours(self, pid):
        return self.base in subprocess.run(["ps", "-E", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout

    def tree(self, pid):
        rows, found, todo = self.ps(), set(), [pid]
        while todo:
            cur = todo.pop()
            for p, (ppid, pgid, _) in rows.items():
                if (ppid == cur or pgid == cur) and p not in found and p != pid:
                    found.add(p)
                    todo.append(p)
        return found

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
