#!/usr/bin/env python3
"""Integration tests against a real, isolated Herdr instance with real TUIs.

    python3 tests/integration/run.py [name-filter]

Needs tmux and herdr 0.9.x on PATH. `gh dash` (installed and logged in) is the real
dashboard; `less` and `top` stand in for the others; `gh board` is configured but not installed.
"""
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from herdr_env import Herdr  # noqa: E402

CONFIG = '''
[personal]
command = "less"
args = ["/etc/hosts"]

[workspaces.disposable-camera]
command = ["gh", "board"]

[workspaces.cloud-config]
command = ["gh", "dash"]
args = ["--config", "GHCFG"]

[workspaces.post-planner]
command = "top"

[workspaces.demo]
command = ["sh", "-c", "echo DEMO-UP; trap 'stty size > $SIZEFILE' WINCH; stty size > $SIZEFILE; echo $$ > $PIDFILE; while :; do sleep 0.1; done"]
env = { SIZEFILE = "SIZE", PIDFILE = "PID" }
'''

results = []
HERDR = None  # the instance under test, so a failed check can dump what the screen showed


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    if not ok and HERDR:
        shown = [l.rstrip()[26:] for l in HERDR.screen().splitlines()]
        print("\n".join("      | " + l[:150] for l in shown if l.strip(" │")), flush=True)
    return ok


def popup_open(h):
    return "┌Dashboard" in h.screen()


def wait_closed(h, timeout=6):
    return h.wait_screen("┌Dashboard", timeout, absent=True)


def open_dashboard(h):
    h.keys("C-b", "d")
    return h.wait_screen("┌Dashboard")


def close_dashboard(h):
    h.keys("C-b", "d")
    return wait_closed(h)


def main(only=None):
    if not shutil.which("tmux") or not shutil.which("herdr"):
        sys.exit("needs tmux and herdr")
    global HERDR
    h = HERDR = Herdr()
    demo_dir = h.base + "/demo"
    os.makedirs(demo_dir)
    ghcfg = h.base + "/gh-dash.yml"
    with open(ghcfg, "w") as f:  # gh-dash 4.24.1 panics in its preview pane under tmux; run it without the preview
        f.write("prSections:\n  - title: My Pull Requests\n    filters: is:open author:@me\ndefaults:\n  preview:\n    open: false\n")
    config = CONFIG.replace("GHCFG", ghcfg).replace('"SIZE"', f'"{demo_dir}/size"').replace('"PID"', f'"{demo_dir}/pid"')
    h.plugin_config = config
    h.start()
    try:
        for label in ("disposable-camera", "cloud-config", "post-planner", "scratch", "demo"):
            h.create_workspace(label, f"{h.base}/ws/{label}")
        h.herdr("workspace", "rename", "w1", "home")
        scenarios = [s for s in SCENARIOS if not only or only in s.__name__]
        for scenario in scenarios:
            h.write_plugin_config(config)
            if popup_open(h):
                h.keys("C-b", "d")
                wait_closed(h)
            h.focus("home")
            h.reset_mode()
            print(f"\n== {scenario.__name__}: {scenario.__doc__.strip().splitlines()[0]}", flush=True)
            try:
                scenario(h)
            except Exception as e:  # a crashed scenario is a failure, not an abort
                check(scenario.__name__ + " (crashed)", False, repr(e))
    finally:
        h.stop()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("kept", h.base)
    else:
        shutil.rmtree(h.base, ignore_errors=True)
    return 1 if failed else 0


def s01_open_each_kind(h):
    """Open the configured Work dashboards (gh dash, top), Personal (less) and the failing gh board."""
    h.focus("cloud-config")
    check("work: gh dash opens in a popup", open_dashboard(h) and h.wait_screen("My Pull Requests", 15))
    check("work: footer names the mode and workspace", h.wait_screen("Work · cloud-config"))
    close_dashboard(h)
    h.focus("post-planner")
    check("work: top opens", open_dashboard(h) and h.wait_screen("Processes:", 8))
    close_dashboard(h)
    h.focus("disposable-camera")
    check("work: gh board (not installed) shows a failure message, popup still opens",
          open_dashboard(h) and h.wait_screen("The dashboard stopped.", 10) and h.wait_screen("Work Dashboard"))
    check("failure message shows gh's own error", h.wait_screen("unknown command"))
    check("failure message offers Tab and Esc", h.wait_screen("Tab  Switch to Personal") and h.wait_screen("Esc  Close"))
    h.keys("Esc")
    check("Esc closes the failure message", wait_closed(h))
    h.focus("scratch")
    check("workspace without a dashboard shows the message",
          open_dashboard(h) and h.wait_screen("No dashboard configured for this workspace."))
    close_dashboard(h)


def s02_switching(h):
    """Switch Personal/Work with the leader chords; Tab and Esc inside gh dash stay with gh dash."""
    h.focus("cloud-config")
    open_dashboard(h)
    h.wait_screen("My Pull Requests", 15)
    h.keys("C-b", "Tab")
    check("Ctrl+B Tab: Work -> Personal (less)", h.wait_screen("127.0.0.1") and h.wait_screen("Personal"))
    h.keys("C-b", "BTab")
    check("Ctrl+B Shift+Tab: Personal -> Work", h.wait_screen("Work · cloud-config", 15))
    check("switching never exposes the terminal underneath (popup border stayed)", popup_open(h))
    h.keys("?")
    helped = h.wait_screen("Navigation", 5) or h.wait_screen("help", 5)
    h.keys("Esc")
    time.sleep(0.8)
    check("Esc reaches gh dash (help closes) and the popup stays open", helped and popup_open(h))
    h.keys("Tab")
    time.sleep(0.5)
    check("bare Tab reaches gh dash, no mode switch", h.wait_screen("Work · cloud-config", 3))
    close_dashboard(h)


def s03_bare_keys_option(h):
    """With [keys] bare = true, Tab / Shift+Tab / Esc are intercepted - and Esc then closes over gh dash's own use."""
    h.write_plugin_config(h.plugin_config + "\n[keys]\nbare = true\n")
    h.focus("cloud-config")
    open_dashboard(h)
    h.wait_screen("My Pull Requests", 15)
    h.keys("Tab")
    check("bare Tab switches to Personal", h.wait_screen("127.0.0.1"))
    h.keys("BTab")
    check("bare Shift+Tab switches back", h.wait_screen("Work · cloud-config", 15))
    h.keys("?")
    h.wait_screen("Navigation", 5)
    h.keys("Esc")
    check("bare Esc closes the popup even though gh dash's help wanted it (documented conflict)", wait_closed(h))


def s04_repeat_toggle_no_duplicates(h):
    """Open and close 10 times with the keybinding; never two popups, never leftovers."""
    h.focus("demo")
    ok, max_sup = True, 0
    pids = set()
    for i in range(10):
        ok &= open_dashboard(h)
        h.wait_screen("DEMO-UP")
        sup = h.supervisors()
        max_sup = max(max_sup, len(sup))
        for s in sup:
            pids |= h.tree(s) | {s}
        h.keys("C-b", "d")
        ok &= wait_closed(h)
        time.sleep(0.3)
    check("10 open/close cycles via Ctrl+B d", ok)
    check("never more than one dashboard process at a time", max_sup == 1, str(max_sup))
    time.sleep(0.5)
    check("no leftover processes after the cycles", not [p for p in pids if h.alive(p)],
          str([p for p in pids if h.alive(p)]))


def s05_action_invocations(h):
    """The action toggles when invoked from the CLI; a second open request is refused by Herdr."""
    h.focus("demo")
    h.herdr("plugin", "action", "invoke", "herdr-dashboard.open")
    check("action invoke opens", h.wait_screen("┌Dashboard"))
    again = h.herdr("plugin", "pane", "open", "--plugin", "herdr-dashboard", "--entrypoint", "dashboard", "--placement", "popup")
    check("Herdr refuses a second popup (ui_busy)", "ui_busy" in str(again), str(again))
    h.herdr("plugin", "action", "invoke", "herdr-dashboard.open")
    check("invoking the action while open closes it (toggle)", wait_closed(h))
    check("no supervisor left", not h.supervisors())


def s06_remembered_mode(h):
    """The spec's example: Disposable Camera -> Personal -> close -> Cloud Config -> open shows Personal -> Tab -> Cloud Config."""
    h.focus("disposable-camera")
    open_dashboard(h)
    h.wait_screen("The dashboard stopped.", 10)
    h.keys("Tab")
    check("1-3. opened in Disposable Camera, switched to Personal", h.wait_screen("127.0.0.1"))
    close_dashboard(h)
    h.focus("cloud-config")
    open_dashboard(h)
    check("4-5. reopened from Cloud Config: Personal is selected", h.wait_screen("127.0.0.1") and h.wait_screen("Personal"))
    h.keys("C-b", "Tab")
    check("6-7. switching shows Cloud Config's gh dash", h.wait_screen("My Pull Requests", 15) and h.wait_screen("Work · cloud-config"))
    close_dashboard(h)


def wait_no_supervisor(h, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not h.supervisors():
            return True
        time.sleep(0.2)
    return False


def s07_workspace_changes(h):
    """Herdr hides a popup when its tab loses focus; the dashboard then stops, and the next open resolves the new workspace."""
    h.focus("demo")
    open_dashboard(h)
    h.wait_screen("Work · demo")
    sup = h.supervisors()
    tree = set(sup)
    for s in sup:
        tree |= h.tree(s)
    h.focus("scratch")
    check("popup is hidden by Herdr when another workspace is focused", h.wait_screen("┌Dashboard", 3, absent=True))
    check("and the dashboard stops instead of running unseen", wait_no_supervisor(h))
    check("with no leftover processes", not [p for p in tree if h.alive(p)])
    check("opening in the new workspace: its own (missing) dashboard, Work mode kept",
          open_dashboard(h) and h.wait_screen("No dashboard configured for this workspace.") and h.wait_screen("Workspace: scratch"))
    ws = h.workspaces()
    h.herdr("workspace", "rename", ws["scratch"], "post-planner")
    check("renaming the workspace while the popup is visible re-resolves Work (top)", h.wait_screen("Processes:", 8))
    h.herdr("workspace", "rename", ws["scratch"], "scratch")
    check("renaming it back shows the message again", h.wait_screen("No dashboard configured for this workspace.", 8))
    close_dashboard(h)
    h.focus("demo")
    open_dashboard(h)
    h.wait_screen("Work · demo")
    h.focus("home")
    h.herdr("workspace", "close", ws["demo"])
    check("closing the popup's workspace leaves no dashboard running", wait_no_supervisor(h))
    h.create_workspace("demo", f"{h.base}/ws/demo")


def s07b_tab_switch(h):
    """A tab switch inside the same workspace hides the popup too."""
    h.focus("demo")
    ws = h.workspaces()["demo"]
    tab2 = h.herdr("tab", "create", "--workspace", ws)["result"]["tab"]["tab_id"]
    h.herdr("tab", "focus", ws + ":t1")
    time.sleep(0.3)
    open_dashboard(h)
    h.wait_screen("Work · demo")
    h.herdr("tab", "focus", tab2)
    check("popup hidden on another tab and its dashboard stopped", h.wait_screen("┌Dashboard", 3, absent=True) and wait_no_supervisor(h))
    h.herdr("tab", "close", tab2)


def s08_focus_layout_and_agents(h):
    """Opening and closing changes no layout and keeps focus and running processes."""
    h.focus("home")
    ws, root = h.create_workspace("busy", f"{h.base}/ws/busy")
    h.focus("busy")
    tick = h.base + "/tick"
    h.herdr("pane", "run", root, f"while :; do date +%s >> {tick}; sleep 0.2; done")
    time.sleep(1)
    before = h.layout_signature()
    n1 = len(open(tick).read().split())
    for _ in range(3):
        open_dashboard(h)
        time.sleep(0.6)
        close_dashboard(h)
    after = h.layout_signature()
    check("layout, focused pane/tab/workspace identical after 3 cycles", before == after)
    time.sleep(1)
    stamps = [int(x) for x in open(tick).read().split()]
    check("the background process in the pane kept running", len(stamps) > n1 + 5, f"{n1}->{len(stamps)}")
    gaps = max(b - a for a, b in zip(stamps, stamps[1:]))
    check("with no stall while the popup was open", gaps <= 1, f"max gap {gaps}s")
    h.type("echo AFTER-CLOSE")
    h.keys("Enter")
    check("keyboard focus is back in the original pane", h.wait_screen("AFTER-CLOSE"))
    h.herdr("pane", "send-keys", root, "ctrl+c")
    h.herdr("workspace", "close", ws)
    h.focus("home")


def s09_stacking_over_overlay(h):
    """The dashboard popup opens above an existing overlay (floating) pane without replacing it."""
    h.focus("demo")
    r = h.herdr("plugin", "pane", "open", "--plugin", "test.overlay", "--entrypoint", "overlay", "--focus")
    check("overlay pane opened", h.wait_screen("OVERLAY-UP") and "plugin_pane_opened" in str(r))
    overlay_pane = r["result"]["plugin_pane"]["pane"]["pane_id"]
    before = h.layout_signature()
    check("Ctrl+B d opens the dashboard over the overlay", open_dashboard(h) and h.wait_screen("DEMO-UP"))
    screen = h.screen()
    check("overlay is still there underneath (its border is visible around the popup)", "Existing float" in screen)
    close_dashboard(h)
    check("closing restores the overlay and its content", h.wait_screen("OVERLAY-UP") and h.wait_screen("Existing float"))
    check("layout unchanged", before == h.layout_signature())
    h.type("typed-into-overlay")
    h.keys("Enter")
    check("focus returned to the overlay pane", h.wait_screen("typed-into-overlay"))
    h.herdr("plugin", "pane", "close", overlay_pane)


def s10_popup_over_popup(h):
    """Herdr allows one popup at a time, and prefix keys never reach Herdr while a popup is focused."""
    h.focus("demo")
    open_dashboard(h)
    h.wait_screen("DEMO-UP")
    r = h.herdr("plugin", "pane", "open", "--plugin", "test.overlay", "--entrypoint", "overlay", "--placement", "popup")
    check("a second popup over the dashboard is refused (ui_busy)", "ui_busy" in str(r), str(r))
    check("Ctrl+B d while the dashboard is open is handled by the dashboard and closes it", close_dashboard(h))
    r = h.herdr("plugin", "pane", "open", "--plugin", "test.overlay", "--entrypoint", "overlay", "--placement", "popup", "--focus")
    check("a foreign popup is open", h.wait_screen("OVERLAY-UP"))
    h.keys("C-b", "d")
    time.sleep(0.8)
    check("with a foreign popup focused, Ctrl+B d does not reach Herdr: no dashboard opens",
          "┌Dashboard" not in h.screen() and "Existing float" in h.screen() or "OVERLAY-UP" in h.screen())
    h.herdr("plugin", "action", "invoke", "herdr-dashboard.open")
    time.sleep(0.8)
    check("invoking the action over a foreign popup does not replace it", "┌Dashboard" not in h.screen())
    logs = h.herdr("plugin", "log", "list", "--plugin", "herdr-dashboard")
    check("the refusal is logged by the action", "ui_busy" in str(logs) or "another popup" in str(logs).lower())
    h.keys("Enter", "C-d")
    check("foreign popup closes on its own exit", h.wait_screen("OVERLAY-UP", 5, absent=True))


def s11_resize(h):
    """Resizing the terminal resizes the running dashboard (footer keeps one row)."""
    h.focus("demo")
    open_dashboard(h)
    h.wait_screen("DEMO-UP")
    size_file = h.base + "/demo/size"
    time.sleep(0.5)
    first = open(size_file).read().split()
    h.resize(160, 40)
    time.sleep(1.2)
    second = open(size_file).read().split()
    check("dashboard saw a new size after the terminal resized", first != second, f"{first} -> {second}")
    check("popup still drawn at the new size", popup_open(h) and h.wait_screen("Work · demo"))
    h.resize(200, 50)
    time.sleep(0.8)
    close_dashboard(h)


def s12_close_while_dashboard_ignores_hangup_and_orphans(h):
    """Closing the popup from Herdr (popup.close) and stopping the server both leave no processes."""
    h.focus("cloud-config")
    open_dashboard(h)
    h.wait_screen("My Pull Requests", 15)
    sup = h.supervisors()
    tree = set()
    for s in sup:
        tree |= h.tree(s) | {s}
    check("gh dash tree captured", len(tree) >= 2, str(len(tree)))
    h.herdr("plugin", "action", "invoke", "herdr-dashboard.open")
    wait_closed(h)
    time.sleep(1.5)
    check("popup.close (via toggle) leaves no gh dash or supervisor process", not [p for p in tree if h.alive(p)],
          str([p for p in tree if h.alive(p)]))
    open_dashboard(h)
    h.wait_screen("My Pull Requests", 15)
    tree = set()
    for s in h.supervisors():
        tree |= h.tree(s) | {s}
    h.run("herdr", "server", "stop")
    time.sleep(3)
    left = [p for p in tree if h.alive(p)]
    check("stopping Herdr while a dashboard runs leaves no orphaned processes", not left, str(left))


def s13_dashboard_exits(h):
    """A dashboard quit by the user closes the popup; one killed behind our back shows the failure screen."""
    h.focus("home")
    open_dashboard(h)  # home has no Work dashboard: switch to Personal (less)
    h.keys("C-b", "Tab")
    check("less runs as the Personal dashboard", h.wait_screen("127.0.0.1"))
    time.sleep(1.2)
    h.keys("q")
    check("quitting the dashboard normally (q in less) closes the popup", wait_closed(h))
    check("and nothing is left running", wait_no_supervisor(h))
    h.reset_mode()
    h.focus("post-planner")
    open_dashboard(h)
    h.wait_screen("Processes:", 8)
    top = [p for s in h.supervisors() for p in h.tree(s) if "top" in h.ps()[p][2].split("/")[-1].split()[0]]
    for p in top:
        os.kill(p, 9)
    check("a dashboard killed from outside shows 'Killed by signal 9' and keeps the popup",
          h.wait_screen("Killed by signal 9.", 8) and popup_open(h))
    h.keys("r")
    check("r restarts it", h.wait_screen("Processes:", 8))
    close_dashboard(h)


def s14_invalid_config(h):
    """Invalid config shows the parse error in both modes and recovers on reload; nothing crashes."""
    good = h.plugin_config
    h.write_plugin_config("[personal\ncommand = ")
    h.focus("demo")
    check("opens despite an invalid config", open_dashboard(h) and h.wait_screen("Invalid configuration."))
    check("shows the TOML error", h.wait_screen("TOML syntax error"))
    h.keys("C-b", "Tab")
    check("Personal shows it too, and the popup stays usable", h.wait_screen("Personal Dashboard") and h.wait_screen("Invalid configuration."))
    h.write_plugin_config(good)
    h.keys("r")
    check("fixing the file and pressing r loads Personal (less)", h.wait_screen("127.0.0.1"))
    h.keys("C-b", "Tab")
    check("Work shows the fixed config's demo dashboard", h.wait_screen("Work · demo"))
    close_dashboard(h)


SCENARIOS = [s01_open_each_kind, s02_switching, s03_bare_keys_option, s04_repeat_toggle_no_duplicates,
             s05_action_invocations, s06_remembered_mode, s07_workspace_changes, s07b_tab_switch,
             s08_focus_layout_and_agents, s09_stacking_over_overlay, s10_popup_over_popup, s11_resize,
             s13_dashboard_exits, s14_invalid_config, s12_close_while_dashboard_ignores_hangup_and_orphans]

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
