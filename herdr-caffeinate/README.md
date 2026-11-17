# herdr-caffeinate

Keeps the Mac from **idle-sleeping only while important background work runs**, lets the display sleep, and releases as soon as the work ends.

Two independent parts, one folder:

| Part | Role |
|---|---|
| **`claude-mod/`**, a Claude Code mod | **Recommended.** Standalone: no herdr, no daemon. Holds a sleep assertion while its own session has a background command or subagent that matters (a build, a test run, a migration, a deploy), and never for a dev server or file watcher. |
| herdr plugin (`herdr-plugin.toml`, `caffeinate.py`, `caffeinate_lib/`, `bin/herdr-caffeinate`) | Optional. A Python daemon that holds sleep while any herdr agent is `working` or a registered task runs. Covers agents other than Claude Code, a Forced mode and a sidebar indicator. For Claude Code agents it duplicates the keep-awake Claude Code already does while a session is busy; it adds nothing for them beyond background tasks, which the mod handles on its own. |

Neither part uses root or `pmset`. Closing the lid still sleeps the Mac.

## The Claude Code mod

Claude Code itself holds `caffeinate -i -t 300` while a session is busy and lets go about 30 s after it goes idle, so a build left running in the background on an idle session does not stop the Mac sleeping. The mod fills that gap.

### Install

```bash
claude plugin marketplace add danielannan17/herdr-plugins   # or the path of a local checkout
claude plugin install herdr-caffeinate@herdr-plugins
```

It needs a Claude Code build with plugin function hooks.

### Behaviour

- While the session has at least one live background task that counts, a single `caffeinate -i -w <Claude Code pid>` child runs (idle system sleep only; the display may still sleep). The kernel drops the assertion when Claude Code exits, however it exits. If the pid cannot be learned the mod runs `caffeinate -i -t 300` and renews it every 240 s instead; that one expires by itself within five minutes of the session dying. There is never more than one.
- The child is released when the last counting task ends, on `session.end` (including `/clear`), and when the mod reloads.
- **What counts:** a background Bash command or background subagent whose class is finite and not marked "need not keep awake", a task you set to `keep`, and a task still unclassified within its 120 s grace. **What does not:** persistent services, anything you set to `ignore`, and unclassified tasks after the grace (the `unknownFallback` option can change that to keep).
- **Completion:** a `task-notification` row ends a task; each `Stop` and `SubagentStop` carries the session's in-flight `background_tasks`, and a task absent from that list (and older than 5 s) is finished; `session.end` clears everything.
- **Long tasks** are never dropped for age. After `stuckAfterHours` (6) the mod shows a toast once and keeps holding. `hardCapHours` (default 0, off) opts in to dropping a task after that long unless you set it to `keep`.

### `/awake`

```text
/awake                  holding or not, and each task with class, source, age and reason
/awake keep <id>        hold for this task regardless of its class
/awake ignore <id>      never hold for this task
/awake clear <id>       remove the override
```

An id can be shortened to a unique prefix. The optional status line (`showStatus`) pins `☕ awake · N background tasks` under the prompt while holding.

### Classification

Who set a task's class matters: pattern rule < the session's own answer < the agent < you. A lower source never overwrites a higher one.

1. **Rules** (`rules.json`, copied into the mod as `hooks/rules.ts`). Persistent patterns first (`npm run dev`, `next dev`, `vite`, `--watch`, `vitest` without `run`, `nodemon`, `tail -f`, anything `serve`, `expo start`, `metro`, `supabase start`, `docker compose up` without `-d`, tunnels, …), then finite ones (`build`, `test`, `jest`, `pytest`, `cargo build|test`, `xcodebuild`, `migrate`, `deploy`, `tsc`, `lint`, `gradle`, …). A command matching neither is classified from the description the agent gave it, only when the description points one way. Your own rules in `~/.config/herdr/plugins/config/danielannan17.caffeinate/rules.json` are tried first.
2. **Ask the agent.** For a command still unknown the mod asks the originating session with `$.model.fork` at low effort: a tool-less one-message question that reuses the session's prompt cache and interrupts nothing. The answer is `{"class","keep_awake","reason"}`, cached per (session, normalized command). When no turn has finished yet (nothing to fork), it is asked again at the next `Stop`.
3. **Fallback** after the 120 s grace.

After editing `rules.json` run `python3 claude-mod/sync-rules.py`; a test fails while the two copies differ.

### Options

`showStatus` (off), `unknownFallback` (`ignore`), `stuckAfterHours` (6), `hardCapHours` (0), `herdrRegistry` (off: also write each task where the herdr plugin's daemon reads it, and wake that daemon; only for running both parts).

### Limits

- A reload of the mod forgets the session's task list; tasks started before it are not held for.
- A background process started outside Claude Code's own Bash and Agent tools is invisible.
- Only Claude Code sessions: other agents are covered by the herdr plugin's `working` detection only.
- Classification by the session's own model is an answer, not a guarantee; `/awake ignore` and `/awake keep` override it.

## The herdr plugin (optional)

Holds one `caffeinate -i -w <daemon pid>` child while any herdr agent is `working` (`idle`, `done`, `blocked` and `unknown` never count), or a registered task is alive and counts, releasing 5 s after the last activity. Modes: Off, Auto, Forced (always). The daemon is event driven and exits, releasing sleep, when herdr's socket is gone or on SIGTERM; `herdr plugin unlink` does not stop a running daemon, so stop it with `pkill -f "caffeinate.py --daemon"`.

```bash
herdr plugin link /path/to/herdr-plugins/herdr-caffeinate
```

To feed it Claude Code's background tasks as well, turn on the mod's `herdrRegistry` option. Without it, background tasks reach the daemon only through `herdr-caffeinate register`.

### herdr config

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

### Modes

| Mode | Behaviour |
|---|---|
| Off | Never holds. |
| Auto | Holds while an agent is `working`, or a registered finite task is alive. Releases 5 s after the last activity. |
| Forced | Always holds. |

The mode is stored in the state dir and restored on startup. A fresh install starts in `default_mode` (Auto).

### Command line

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

### Configuration

Copy `config.example.toml` to `~/.config/herdr/plugins/config/danielannan17.caffeinate/config.toml`. Options: `default_mode`, `release_grace_sec`, `unknown_grace_sec`, `unknown_fallback`, `stuck_after_hours`, `hard_cap_hours`, `notify_mode_change`, `notify_stuck`, `tick_sec`. Extra classification rules go in `rules.json` in the same folder (same shape as the shipped `rules.json`; yours are tried first).

### How it decides

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

#### Classification in the daemon

The daemon does not classify. `herdr-caffeinate register` applies the same `rules.json` rules when no `--class` is given, and the mod (with `herdrRegistry` on) writes the class it settled on. A task's class source ranks `rule` < `fork` < `agent` < `cli`/`user`: a lower source never overwrites a higher one, and your `override keep|ignore` beats everything. An unclassified task counts for `unknown_grace_sec`, then `unknown_fallback` applies.

## herdr plugin: state and files

`~/.local/state/herdr/plugins/danielannan17.caffeinate/` (what herdr passes as `HERDR_PLUGIN_STATE_DIR`): `mode`, `tasks/`, `snapshots/`, `status.txt`, `cli-path` (so the mod can wake the daemon), `daemon.lock`, `poke.fifo`, `daemon.log`.

## herdr plugin: safety

- The only thing that holds sleep is a `caffeinate -i -w <daemon pid>` child. The kernel drops the assertion when the daemon dies, including `kill -9`.
- The daemon exits, releasing it, when herdr's socket is unreachable three checks in a row (about 30 s) and on SIGTERM.
- Off releases at once. Forced holds with nothing else to justify it, so leave it only on purpose.
- A registered finite task with no pid and no session can hold until you `complete` or `override` it; the stuck warning at 6 h is the only automatic signal.

## herdr plugin: known limitations

- Agents other than Claude Code only contribute `working`/`idle`; their background work is invisible unless registered with `herdr-caffeinate register`.
- Status is detected from the screen, so there is a lag, and an agent can read as `idle` between steps; the 5 s grace bridges short gaps only.
- A permission prompt reads as `blocked` and does not hold the Mac awake, by design.
- Pid liveness is `kill -0`, so a recycled pid can keep a task alive until its completion is reported.
- The mod writes task files without a rename (when `herdrRegistry` is on); the daemon treats an unreadable file as absent for one pass.
- Closing the lid sleeps the Mac regardless; this plugin does not touch `pmset`.

## Tests

```bash
python3 -m unittest discover -s tests -t .      # from this folder
claude plugin validate claude-mod
claude plugin test claude-mod
```

Type-check the mod with `tsc -p claude-mod` once the engine has laid `claude-mod/.claude-plugin/types/` (it does when a session loads the mod).
