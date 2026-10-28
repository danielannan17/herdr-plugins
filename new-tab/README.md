# New Tab

A workspace-aware **Create New Tab** popup for [Herdr](https://herdr.dev) 0.9+. It replaces `Ctrl+B` `C`.

```
┌Create New Tab──────────────────────────────────────────┐
│                                                        │
│ Workspace: api                                    [⌄]  │
│ Shell: Claude                                          │
│ Tab name: █                                default: 3  │
│                                                        │
│                                                        │
│                                                        │
│ Enter to create · Alt+↓ workspace · Shift+Tab shell    │
└────────────────────────────────────────────────────────┘
```

`Ctrl+B` `C` `Enter` creates a tab in the current workspace, as before. `Alt+↓` opens a numbered workspace list so the tab can go to any other workspace without leaving the keyboard. `Shift+Tab` picks which shell the tab starts, from the list in `shells.toml`.

## Keys

| Key | Effect |
| --- | --- |
| typing | edits the tab name (digits are name text unless the list is open) |
| `Enter` | creates the tab in the chosen workspace, switches Herdr to it, closes the popup |
| `Alt+↓` (or `Tab`) | opens the workspace list |
| `Shift+Tab` | cycles to the next shell (wraps) |
| `Esc` | cancels, nothing is created |
| list: `1`-`9` | selects that workspace and returns to the name field |
| list: `↑` `↓` `Home` `End` | moves the highlight (wraps, scrolls past nine workspaces) |
| list: `Enter` | selects the highlighted workspace |
| list: `Esc` | closes the list only |
| name field: `←` `→` `Home` `End` `Ctrl+U` `Ctrl+W` `Backspace` `Delete` | edit |

In the list, `✓` marks the destination and `· active` marks the workspace Herdr is showing, so the two are distinguishable when they differ. Choosing a workspace changes only the destination; Herdr does not switch until the tab is created.

## Shells

The popup offers the shells listed in `shells.toml` in the plugin's Herdr config folder (`herdr plugin config-dir danielannan17.new-tab` prints it; `~/.config/herdr/plugins/config/danielannan17.new-tab/` by default). The first entry is the default. The file is created the first time the popup opens, as a copy of `shells.example.toml` from this plugin. Edit that copy to change your shells; it is never overwritten, and updating the plugin leaves it alone.

```toml
[[shell]]
name = "Claude"
command = "c"

[[shell]]
name = "zsh"
```

A `command` is typed, followed by `Enter`, into the new tab's interactive shell (the API call behind `herdr pane run`), so aliases and functions such as `c` resolve. An entry without a `command` leaves the plain shell alone. The file is read each time the popup opens. If the file is unreadable or invalid, the popup shows the reason on its error line and offers one plain shell, so a tab can still be created. If the file cannot be created, the popup shows that on its error line and offers the shells from the example for that opening. `NEW_TAB_SHELLS` points the plugin at a different file and skips the config folder.

If typing the command fails, the tab already exists: the popup says so and only `Enter` or `Esc` (close) are accepted, so a second tab cannot be created by accident. Success is acknowledged when Herdr accepts the input, not when the command finishes; the command may still fail inside the tab.

## Behaviour that mirrors Herdr's own dialog

- The destination starts as the focused workspace and is never remembered between openings.
- An empty name, or a name equal to Herdr's default (tabs in the workspace plus one, shown as `default: N`), sends no label, so Herdr names the tab itself.
- The tab is created with `focus: true`, with no `cwd` and no `env`, so Herdr resolves the working directory from the destination workspace's focused pane.
- At `Enter` the workspace list is read again and the destination is looked up by its stable id. If the workspace was closed meanwhile the dialog stays open, shows the refreshed list and asks for a new pick. If creation fails the dialog stays open with the error and the typed name.
- Keys that arrive while a tab is being created are ignored and flushed, so a repeated `Enter` cannot create two tabs.

## Install

Requires Herdr 0.9.0 or newer and `python3` (3.11+ for `tomllib`, standard library only; on an older interpreter the popup still works but ignores `shells.toml` and says so) on the `PATH` that Herdr runs plugins with.

```bash
herdr plugin install danielannan17/herdr-plugins/new-tab
# or, from a checkout:
herdr plugin link /path/to/herdr-plugins/new-tab
```

Add to `~/.config/herdr/config.toml`. Moving the built-in `new_tab` off `prefix+c` keeps the native dialog available on `Ctrl+B` `Shift+C`:

```toml
[keys]
new_tab = "prefix+shift+c"

[[keys.command]]
key = "prefix+c"
type = "plugin_action"
command = "danielannan17.new-tab.open"
description = "new tab (choose workspace)"
```

Put `[keys]` before your first `[[keys.command]]` entry, then reload:

```bash
herdr config check
herdr server reload-config
```

A user-set `keys.new_tab` silently displaces the default `prefix+c`, so there is no conflict diagnostic; `herdr config check` should print `config: ok`.

## Uninstall

Delete the `[keys]` `new_tab` line and the `prefix+c` `[[keys.command]]` block from `config.toml` (that restores the default `prefix+c`), then:

```bash
herdr server reload-config
herdr plugin unlink danielannan17.new-tab
```

## Why Python

The popup is a small form that runs once per use. Python's standard library gives a fast start (about 55 ms to first paint, measured), needs no build step, and keeps the plugin a plain directory that `herdr plugin link` can use directly. A Rust binary would start a few milliseconds faster but would add a toolchain and a build per Herdr upgrade. Terminal handling uses `termios` and plain escape sequences rather than `curses`, because `curses` hides the escape timeout that separating `Esc` from `Alt+↓` depends on.

Layout of the code:

- `newtab/logic.py` state machine, default-name and label rule, workspace resolution (pure)
- `newtab/keys.py` terminal bytes to key names (pure)
- `newtab/render.py` state to screen rows (pure)
- `newtab/herdr.py` the Herdr API socket
- `newtab/shells.py` finds, first-run creates and reads `shells.toml`
- `shells.example.toml` the shipped default shells
- `newtab/app.py` the terminal loop

The action asks Herdr for the popup directly (`herdr plugin pane open`), so no interpreter starts before the popup does. The API socket is used instead of the CLI because the CLI's parser cannot carry a tab name that starts with `-`.

## Tests

```bash
python3 -m unittest tests.test_logic tests.test_keys tests.test_render tests.test_shells
python3 -W ignore -m unittest tests.integration.test_popup tests.integration.test_flow
```

The integration tests start a private Herdr server (own config dir and sockets under a temp directory, scrubbed environment) and, for `test_flow`, attach a real Herdr client on a pty. They never touch a running session. `test_flow` also copies `~/.config/herdr/config.toml` into the private server, applies the change above and checks that `herdr config check` reports no new diagnostics.

## Limitations

- Workspaces beyond the ninth are reachable with the arrow keys only; `1`-`9` select the first nine.
- The popup has a fixed size (56 by 10 cells, eight inner rows). With many workspaces the list scrolls.
- `Esc` and `Alt+↓` are told apart by a 50 ms timeout; over a very slow remote link a split `Alt+↓` could read as `Esc`. `Tab` opens the list too.
- The popup opens about 100 ms after `C` (Herdr runs the action, then starts the program). An `Enter` pressed sooner than roughly 80 ms after `C` reaches the pane underneath instead of the popup. Typing at normal speed does not do this; Herdr's own dialog, now on `Ctrl+B` `Shift+C`, opens in the same frame.
- Herdr popups are modal and singleton: nothing else can open while this one is up, and opening it while Settings or copy mode is active fails with `ui_busy`.
- Herdr's popup frame is drawn by Herdr, so the title bar and border follow your Herdr theme.
- Unix only (Linux, macOS).
