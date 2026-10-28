# Dashboard

A Herdr plugin that opens a Personal or Work dashboard in a floating popup. Personal is the same everywhere. Work is the dashboard of the workspace you opened it from: `gh board` for one repository, `gh dash` for others, any terminal program you like.

`Ctrl+B d` opens it over whatever you were doing and `Ctrl+B d` closes it. The terminals and agents underneath keep running, and the layout does not change.

Requires Herdr 0.9.0 or newer, Python 3.11 or newer (`tomllib`), and nothing else. No dependencies are installed.

## Install

```bash
# 1. Register the plugin (Herdr does not need to be running)
herdr plugin install danielannan17/herdr-plugins/dashboard
# or, from a checkout:
herdr plugin link /path/to/herdr-plugins/dashboard

# 2. Configure the dashboards
cp /path/to/herdr-plugins/dashboard/config.example.toml "$(herdr plugin config-dir herdr-dashboard)/config.toml"
```

3. Bind the key in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+d"
type = "plugin_action"
command = "herdr-dashboard.open"
description = "dashboard"
```

4. Reload: `herdr server reload-config`.

`prefix+d` is unbound in Herdr 0.9.0 (`close_workspace` is `prefix+shift+d`). To uninstall, remove the binding and run `herdr plugin unlink herdr-dashboard`.

## Keys

Herdr's prefix keys do not work while a popup has focus (see "What Herdr allows"), so the popup implements its own prefix. It reads Herdr's `keys.prefix` and the `prefix+<key>` you bound to `herdr-dashboard.open`, so with the defaults:

| Keys | Does |
| --- | --- |
| `Ctrl+B d` (outside the popup) | open the dashboard for the saved mode |
| `Ctrl+B d` (inside) | close it |
| `Ctrl+B Tab` | switch to the other mode |
| `Ctrl+B Shift+Tab` | switch the other way (with two modes, the same as Tab) |
| `Ctrl+B p` / `Ctrl+B w` | go straight to Personal / Work |
| `Ctrl+B r` | re-read `config.toml` and restart the current dashboard |
| `Ctrl+B Ctrl+B` | send a literal `Ctrl+B` to the dashboard |

On the message screen (nothing to run, or it failed) the bare keys work too, because nothing there uses them: `Tab` / `Shift+Tab` switch, `Esc` closes, `r` retries.

Bare `Tab`, `Shift+Tab` and `Esc` are not intercepted while a dashboard runs, because dashboards use them: `gh dash` uses `Esc` to leave search, help and comment boxes and `Tab` to accept completions, and `lazygit` (launched from `gh dash`) uses both everywhere. Intercepting them would close the dashboard when you only meant to leave a search box. If a dashboard of yours does not need them, opt in:

```toml
[keys]
bare = true            # every dashboard

[workspaces.some-repo]
command = "my-tui"
bare_keys = true       # only this one
```

A chord that arrives in a different encoding (`ESC [ Z`, kitty `CSI 9;2u`, xterm `CSI 27;2;9~`) is recognised in all of them.

## Behaviour

- **Mode** is Personal or Work, saved globally (not per workspace) in the plugin's state directory. Open in Disposable Camera, switch to Personal, close, switch to Claude Config, open: Personal is shown; `Ctrl+B Tab` shows Claude Config's dashboard. The first ever mode is Work.
- **Work** resolves the workspace at open time and again on every switch, restart and rename.
- **Nothing is configured, or the workspace has no dashboard**: a box titled "Personal Dashboard" or "Work Dashboard" says so, with `Tab Switch to ...` and `Esc Close`. The popup always opens, the other mode stays one key away, and it never switches by itself.
- **Missing executable, bad working directory, invalid config, a command that fails or exits unexpectedly**: the same box with the cause (the command line, the exit status or signal, the first and last lines of its output, the TOML error) and `r Reload config and retry`. A dashboard that exits with status 0 after running for a second or more (you pressed `q`) closes the popup instead.
- **Config is re-read** on every open, switch and `Ctrl+B r`. Edit, press `Ctrl+B r`.
- **The footer** (one reversed row at the bottom) shows the mode, the workspace, and the Tab and close keys. The dashboard gets one row less than the popup. `[ui] footer = false` removes it.
- **The popup follows its tab.** Herdr hides a popup whenever a different tab is focused (also in another workspace). A hidden dashboard would keep running unseen, so it is stopped instead; the next open resolves the workspace you are in. A workspace renamed while the popup is visible is re-resolved.
- **Closing** (any way) kills the dashboard's process group: SIGHUP, then SIGTERM, then SIGKILL, and a dashboard killed along with the popup is hung up by its terminal. Closing Herdr does the same. Nothing runs while the popup is closed; there is no daemon.

## Configuration

`$(herdr plugin config-dir herdr-dashboard)/config.toml`; see [config.example.toml](config.example.toml) for every key.

```toml
[personal]
# command = "my-personal-tui"

[workspaces.disposable-camera]
command = ["gh", "board"]

[workspaces.claude-config]
command = ["gh", "dash"]
args = ["--config", "~/.config/gh-dash/claude-config.yml"]
env = { GH_FORCE_TTY = "1" }
cwd = "~/Projects/claude-config"
```

`command` is a string (split like a shell line) or an argv list; `args`, `env` and `cwd` are optional; a leading `~` in `command`, `args` and `cwd` is expanded. The command runs with `HERDR_DASHBOARD_MODE` and `HERDR_DASHBOARD_WORKSPACE` set, which a custom TUI can read.

### Which workspace is which

Herdr gives a workspace an id (`w1`), a label, and nothing else that identifies it. The id is a position in the session, and a workspace has no path of its own: the closest thing is the current directory of its focused pane, which changes when you `cd`. So there is no durable identifier to key on. The plugin keys on the **label** (what the sidebar shows), compared ignoring case and punctuation, and lets an entry add more matchers that survive a rename:

```toml
[workspaces.disposable-camera]
label = ["disposable-camera", "camera"]      # default: the key
path  = "~/Projects/disposable-camera"       # also matches a workspace whose focused pane is inside this directory
```

The label is tried first, then the longest matching path (`~/Projects/app` does not match `~/Projects/app-web`). A workspace deleted or renamed without a matching `label` or `path` simply has no dashboard, and shows the message.

## Tests

```bash
python3 -m unittest discover -s tests -t .        # unit tests and tests of the popup process on a real pty (about 15 s)
python3 tests/integration/run.py [filter]         # needs tmux, herdr and gh dash
```

The integration run starts its own Herdr (separate `XDG_CONFIG_HOME` and `XDG_STATE_HOME`, own socket, hosted in tmux) and never touches your session. It drives real `Ctrl+B` chords through tmux against real programs: `gh dash`, `less`, `top`, an overlay pane for stacking, and `gh board` (not installed, which exercises the failure screen).

To see what the popup received and decided, create a file named `debug` in the plugin's state directory (`~/.local/state/herdr/plugins/herdr-dashboard/`); the popup then appends to `debug.log` beside it.

## What Herdr allows

Findings from Herdr 0.9.0, each reproduced in an isolated instance.

| Question | Answer |
| --- | --- |
| Can a popup float above another floating window? | Above an **overlay** pane: yes (a popup opens over it, and focus returns to it on close). Above another **popup**: no. A popup is a session singleton and a second open fails with `ui_busy: a popup pane is already open`. The plugin never replaces someone else's popup; it shows a Herdr notification instead. |
| Do Herdr prefix bindings fire while a popup has focus? | No. The popup receives every byte, including `Ctrl+B`. This is why the popup has its own prefix, and why the open shortcut toggles from inside the popup rather than through Herdr. |
| Can the popup title change? | No. OSC 0/2 are ignored; the title comes from the manifest. The footer shows the mode instead. |
| Does a popup survive focusing another tab or workspace? | It is hidden, not closed, while another tab has focus, and its process keeps running. The plugin stops it. |
| Can a popup's size be set per open? | Yes, through the socket (`plugin.pane.open` with `width`/`height`), not through the CLI. `[popup]` in the config uses the socket. |
| Is there a stable workspace id? | No (see above). |
| How does Herdr close a popup? | `popup.close` sends SIGHUP to its process; `herdr plugin pane close` cannot (a popup has no pane id), so the plugin uses the socket. |

## Limitations

- **Stacking over another popup** is impossible in Herdr 0.9.0. Smallest alternative: keep the overlay pane (type `pane` in a key command or an overlay plugin pane) for the thing the dashboard should sit above. Anything that uses a popup (herdr-bar, easyjump, `type = "popup"` key commands) has to be closed first.
- **No Herdr-native mode switch** key: Herdr does not deliver keys to a plugin while its popup is open. The leader chord is the replacement.
- **The footer is written over the dashboard's last row**, which it reserves by shrinking the dashboard's terminal by one row. It is redrawn at most every 0.2 s while the dashboard outputs; it uses the terminal's save/restore cursor, which a program using those for itself would notice. `[ui] footer = false` turns it off.
- **`Ctrl+B` is taken from dashboards** (as it is from everything inside Herdr); `Ctrl+B Ctrl+B` passes it through. `Ctrl+B` is also how `less` pages up, for example.
- **A dashboard cannot be paused**: hiding the popup stops it, so its state (scroll position, filters) is lost.
- **`gh dash` 4.24.1 panics in its preview pane** in the tmux-hosted test terminal (`markdown.GetMarkdownRenderer`, a nil renderer), outside Herdr as well. The message screen shows the crash. The integration tests run it with the preview closed (`defaults.preview.open: false` in a gh-dash config).
