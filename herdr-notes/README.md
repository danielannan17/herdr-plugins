<div align="center">

# Herdr Notes

### The scratch note that lives beside your agents.

A markdown note per workspace, plus one global note, in a dockable
[herdr](https://github.com/ogulcancelik/herdr) pane — rendered preview,
plain-text editing, and it never forgets: everything autosaves and survives
computer restarts.

<img alt="Rust" src="https://img.shields.io/badge/Rust-self--contained_crate-orange?logo=rust&logoColor=white">
<img alt="herdr" src="https://img.shields.io/badge/herdr-%E2%89%A5%200.7-5865a3">
<img alt="Platforms" src="https://img.shields.io/badge/Windows%20%C2%B7%20macOS%20%C2%B7%20Linux-supported-2ea44f">
<img alt="CI" src="https://github.com/alexarthurs/herdr-notes/actions/workflows/ci.yml/badge.svg">
<img alt="License" src="https://img.shields.io/badge/license-MIT-blue">

<br><br>

<img src="docs/media/hero.png" alt="The Notes pane docked beside a running test suite: rendered markdown with headings, checkboxes, code and quotes" width="920">

</div>

Forked from [alexarthurs/herdr-notes](https://github.com/alexarthurs/herdr-notes) (MIT).

That's the note docked on the right, keeping its shape while the test suite runs
next door. Terminals are where the work happens, and the work generates thoughts —
half-finished todo lists, commands you keep retyping, things to ask about later.
This gives them a permanent home one keypress away: no editor window, no stray
`notes.txt`, no saving.

```
herdr plugin install alexarthurs/herdr-notes
```

---

## Why you'll keep it open

- **Rendered markdown** — headings, checkboxes, lists, quotes, code blocks
  and inline styles, drawn natively in the terminal with a scrollbar.
- **Zero-friction editing** — `e` to type, `Esc` to go back. That's it.
- **A note per workspace, and a global one** — every herdr space keeps its
  own note, keyed to the workspace itself (renames don't lose it), and a
  second note is shared by all of them. A tab bar at the top switches
  between the two.
- **Actually persistent** — atomic autosaves to a per-workspace JSON file in
  herdr's config directory. Close the pane, kill the terminal, reboot: it
  comes back.
- **A polite pane** — one toggle action opens, focuses, or closes it; a
  heartbeat token means a dead pane — even one left behind by a herdr
  server restart — gets replaced on the next toggle, never duplicated.

## Install

From a checkout of this repo:

```
cargo build --release
herdr plugin link .
```

Or straight from GitHub with the command under the hero image.

## Open

One toggle action, scoped to the current tab — it opens the pane docked on
the right edge, focuses it if it's already open, and closes it if it's focused:

```
herdr plugin action invoke herdr-notes.open-notes-windows   # windows
herdr plugin action invoke herdr-notes.open-notes           # linux / macos
```

First run greets you with the keymap:

<div align="center">
<img src="docs/media/welcome.png" alt="Empty note showing the built-in key reference" width="920">
</div>

## Keys

Preview (default):

| Key | Action |
| --- | --- |
| `e` / `Enter` | edit the note |
| `Tab` | switch between the Workspace and Global notes |
| `1` / `2` | show the Workspace / Global note |
| `Up` `Down` `PgUp` `PgDn` | scroll |
| `g` / `G` | jump to top / bottom |
| `x` | clear the note (y/N confirm) |
| `q` | quit |

Edit:

| Key | Action |
| --- | --- |
| `Esc` | back to preview (saves) |
| `Ctrl+S` | save now (autosave runs anyway, ~2s after the last keystroke) |

`Esc` never exits the app. In edit mode `Tab`, `1` and `2` are text, so press
`Esc` first to switch notes.

<div align="center">
<img src="docs/media/edit.png" alt="Edit mode: the same note as plain markdown with a block cursor" width="920">
</div>

## Workspace and global notes

The top row shows two tabs, ` Workspace ` and ` Global `, with the active one
highlighted, followed by the mode and scroll position. The Workspace tab is the
note of the current herdr workspace; the Global tab is one note shared by every
workspace. Switching saves the note you leave, then shows the other with the
mode it was saved in. `x` and everything else act on the active note only.

The selected tab is remembered in `_view.json` (`{"tab":"global"}`), so the pane
opens on the tab you left it on. The pane opens on Workspace when that file is
missing or unreadable.

## Persistence

Each herdr workspace gets its own note, stored as
`<workspace-id>.json` in herdr's per-plugin state directory
(`HERDR_PLUGIN_STATE_DIR` — e.g. `%LOCALAPPDATA%\herdr\plugins\herdr-notes\`
on Windows), keyed by the stable `HERDR_WORKSPACE_ID` herdr injects into
every pane — ids survive workspace renames, so the note follows the
workspace, not its label. Closing a workspace just orphans its file; delete
`<workspace-id>.json` by hand if you want it gone. Run outside herdr, the
pane falls back to `herdr/notes/` under the platform config dir (single
shared `notes.json` when there's no workspace id), and any note found in
the fallback layout is moved into the state dir on first load — an
existing note is inherited, never lost.

The global note is `_global.json` in the same directory (`herdr/notes/` under
the config dir in the fallback layout). The leading underscore keeps it from
ever colliding with a workspace id, which is alphanumeric. `_view.json` sits
beside it.

The format is `{ "text": "...", "mode": "preview"|"edit" }`. Saves are
atomic (temp file + fsync + rename) and happen on leaving edit mode, clear,
quit, and debounced while typing. A missing or corrupt file falls back to
an empty note — it never wedges the pane.

The pane re-reads the active note's file about once a second. When something
else has rewritten it — another Notes pane showing the same global note, or a
script — and you have no unsaved edits, the new text appears. While you are
mid-edit with unsaved changes the pane leaves the file alone and your next
autosave overwrites it (last writer wins).

## Hacking

`CLAUDE.md` has the build/dev workflow and the hard-won herdr/Windows
gotchas (pane spawning, heartbeats, PowerShell 5.1 quirks). The short
version: `cargo build --release`, `cargo test`,
`cargo clippy --all-targets -- -D warnings`, all green before shipping.
