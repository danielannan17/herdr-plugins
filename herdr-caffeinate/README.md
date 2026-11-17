# herdr-caffeinate

Keeps the Mac from **idle-sleeping only while important agent work runs**, lets the display sleep, releases a few seconds after the work ends, and shows why it is awake.

- A `working` agent keeps the Mac awake. `idle`, `done`, `blocked` (a permission prompt or a question waiting on you) and `unknown` do not.
- **Finite** background work (builds, tests, migrations, deploys, codegen, background subagents) started by an agent keeps it awake until it finishes, fails or is killed, even after that agent has gone idle.
- **Persistent** work (dev servers, file watchers, tunnels) never does.
- Unclassified work counts for a short grace period, then falls back to a configured default (ignore).

It holds sleep with one `caffeinate -i -w <daemon pid>` child: idle system sleep only, no root, no `pmset`. Closing the lid still sleeps the Mac.

## Parts

| Part | What it is |
|---|---|
| herdr plugin (`herdr-plugin.toml`, `caffeinate.py`, `caffeinate_lib/`) | Python 3 standard library only. A singleton daemon woken by herdr events, the mode actions, the details popup. |
| `bin/herdr-caffeinate` | The command line: status, details, mode, register tasks by hand, override. |
| `claude-mod/` | A Claude Code plugin ("mod"). Reports each background command and subagent a Claude Code session starts, classifies it, and reports its completion. Optional; without it only agent `working`/`idle` counts. |

## Install

```bash
herdr plugin link /path/to/herdr-plugins/herdr-caffeinate
# or: herdr plugin install danielannan17/herdr-plugins/herdr-caffeinate
```

The Claude Code mod installs from this repository's marketplace:

```bash
claude plugin marketplace add danielannan17/herdr-plugins   # or the local checkout path
claude plugin install herdr-caffeinate@herdr-plugins
```

The mod needs a Claude Code build with plugin function hooks (`claude plugin validate` and `claude plugin test` exist). Its optional status line under the prompt is the `showStatus` option.

`herdr plugin unlink danielannan17.caffeinate` removes the plugin; the daemon releases sleep and exits when herdr's socket disappears or it receives SIGTERM.

## herdr config

Keys (pick ones free in your config; `prefix+?` lists the bindings in use):

```toml
[[keys.command]]
key = "prefix+shift+c"
type = "plugin_action"
command = "danielannan17.caffeinate.cycle"
description = "caffeinate: cycle mode"

[[keys.command]]
key = "prefix+shift+i"
type = "plugin_action"
command = "danielannan17.caffeinate.details"
description = "caffeinate: details"
```

Indicator. The daemon sets the workspace metadata token `caffeinate` on every workspace (for example `☕ Awake · 2 agents · 1 build`). Add it to a space row:

```toml
[ui.sidebar.spaces]
rows = [["state_icon", "workspace"], ["branch", "git_status"], [{ token = "$caffeinate", fg = "#f1fa8c" }]]
```

The same text is in `~/.local/state/herdr/plugins/danielannan17.caffeinate/status.txt`. The token expires by itself 150 s after the daemon stops refreshing it.

Actions: `cycle` (Off, Auto, Forced, Off), `off`, `auto`, `forced`, `details` (popup). Invoke any with `herdr plugin action invoke danielannan17.caffeinate.<action>`.

## Modes

| Mode | Behaviour |
|---|---|
| Off | Never holds. |
| Auto | Holds while an agent is `working`, or a registered finite task is alive. Releases 5 s after the last activity. |
| Forced | Always holds. |

The mode is stored in the state dir and restored on startup. A fresh install starts in `default_mode` (Auto).

## Command line

```bash
herdr-caffeinate status
herdr-caffeinate details                 # Active / Possibly stuck / Unclassified / Ignored, with a reason each
herdr-caffeinate mode [off|auto|forced|cycle]
herdr-caffeinate register --task-id build1 --session <claude session id> --command "make" \
    --description "release build" --class finite --pid 12345 --timeout 7200
herdr-caffeinate update --task-id build1 --session <id> --class persistent
herdr-caffeinate complete --task-id build1 --session <id> [--status failed]
herdr-caffeinate override <task-id | session:task-id> keep|ignore|clear
```

`register` without `--class` classifies with the rules. A task with neither `--pid`, `--session` nor `--timeout` is only ended by `complete` or an override, and shows as possibly stuck after `stuck_after_hours`.

## Configuration

Copy `config.example.toml` to `~/.config/herdr/plugins/config/danielannan17.caffeinate/config.toml`. Options: `default_mode`, `release_grace_sec`, `unknown_grace_sec`, `unknown_fallback`, `stuck_after_hours`, `hard_cap_hours`, `notify_mode_change`, `notify_stuck`, `tick_sec`. Extra classification rules go in `rules.json` in the same folder (same shape as the shipped `rules.json`; yours are tried first).

## How it decides

The daemon is event driven. herdr runs `caffeinate.py poke` on `pane.agent_status_changed`, `pane.agent_detected`, `pane.exited` and `pane.closed`; the poke wakes the daemon through a FIFO, and it re-reads `herdr agent list` and the task registry. Without events it wakes only for the end of a release grace or an unclassified task's grace, every 30 s for a full re-check, and every 10 s for a cheap probe of herdr's socket.

Agent status comes from herdr's screen detection, so a status change lags the screen by a moment.

### Tasks and the registry

Each background task is one JSON file in `<state dir>/tasks/`, keyed by (session id, task id), so the same command in two sessions is two tasks. The Claude Code mod and the CLI write them; the daemon deletes the ones it drops. A task is dropped when:

- its completion was reported (a `task-notification`, `complete`, or its session ended);
- its recorded pid is dead;
- its Claude session no longer appears in `herdr agent list` and it has no live pid;
- the session's latest `Stop` snapshot no longer lists it (and it is older than 5 s);
- its `--timeout` passed, or `hard_cap_hours` (if set) passed and you have not set it to `keep`.

A long task is **never** dropped for age alone. After `stuck_after_hours` (6) it is listed under "Possibly stuck", shown as `⚠ N stuck` in the indicator and notified once, and it keeps the Mac awake until it finishes or you `override … ignore` it. A persistent task whose agent said "finite" is treated the same way: nothing special beyond the stuck warning.

### Classification

Who set a task's class matters: `rule` < `fork` < `agent` < `cli`/`user`. A lower source never overwrites a higher one, so an agent's own statement beats a pattern rule, and your `override keep|ignore` beats everything.

1. **Rules** (`rules.json`, shared by the CLI and the mod). Persistent patterns are tried first (`npm run dev`, `next dev`, `vite`, `--watch`, `vitest` without `run`, `nodemon`, `tail -f`, anything `serve`, `expo start`, `metro`, `supabase start`, `docker compose up` without `-d`, tunnels, …), then finite ones (`build`, `test`, `jest`, `pytest`, `cargo build|test`, `xcodebuild`, `migrate`, `deploy`, `tsc`, `lint`, `gradle`, …). A command that matches neither is classified from the description the agent gave it, only when the description points one way.
2. **Ask the agent.** For a command still unknown, the mod asks the originating session with `$.model.fork` at low effort: a tool-less one-message question that reuses the session's prompt cache and interrupts nothing. The answer is `{"class","keep_awake","reason"}`, cached per (session, normalized command). If no turn has finished yet (nothing to fork), the question is retried at the next `Stop`.
3. **Fallback.** Unanswered, a task counts for `unknown_grace_sec`, then `unknown_fallback` applies.

After editing `rules.json` run `python3 claude-mod/sync-rules.py` to regenerate the mod's copy; a test fails when the two differ.

## State and files

`~/.local/state/herdr/plugins/danielannan17.caffeinate/` (what herdr passes as `HERDR_PLUGIN_STATE_DIR`): `mode`, `tasks/`, `snapshots/`, `status.txt`, `cli-path` (so the mod can wake the daemon), `daemon.lock`, `poke.fifo`, `daemon.log`.

## Safety

- The only thing that holds sleep is a `caffeinate -i -w <daemon pid>` child. The kernel drops the assertion when the daemon dies, including `kill -9`.
- The daemon exits, releasing it, when herdr's socket is unreachable three checks in a row (about 30 s) and on SIGTERM.
- Off releases at once. Forced holds with nothing else to justify it, so leave it only on purpose.
- A registered finite task with no pid and no session can hold until you `complete` or `override` it; the stuck warning at 6 h is the only automatic signal.

## Known limitations

- Agents other than Claude Code only contribute `working`/`idle`; their background work is invisible.
- A background process started outside Claude Code is invisible unless registered with `herdr-caffeinate register`.
- Status is detected from the screen, so there is a lag, and an agent can read as `idle` between steps; the 5 s grace bridges short gaps only.
- A permission prompt reads as `blocked` and does not hold the Mac awake, by design.
- Pid liveness is `kill -0`, so a recycled pid can keep a task alive until its completion is reported.
- The mod writes task files without a rename; the daemon treats an unreadable file as absent for one pass.
- Closing the lid sleeps the Mac regardless; this plugin does not touch `pmset`.

## Tests

```bash
python3 -m unittest discover -s tests -t .      # from this folder
claude plugin validate claude-mod
claude plugin test claude-mod
```

Type-check the mod with `tsc -p claude-mod` once the engine has laid `claude-mod/.claude-plugin/types/` (it does when a session loads the mod).
