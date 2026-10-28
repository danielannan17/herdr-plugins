# Agent Smart Rename — herdr plugin

Names each agent session after the work it is doing, so herdr's agents panel
reads `scrape-tweets` instead of a column of identical `claude` rows.

herdr already supports a per-agent name (`herdr agent rename`), and the panel's
`agent` row renders it in preference to the detected agent label. Nothing
populates it automatically. This does.

```
before                        after
● scrape tweets               ● scrape tweets
  claude                        scrape-tweets
● scrape tweets               ● scrape tweets
  claude                        dedupe-thread-order
● lightbulb                   ● lightbulb
  codex                         open-review-pr
```

Sibling plugin: [`herdr-tab-smart-rename`](https://github.com/iurysza/herdr-tab-smart-rename)
names *tabs* on its own; this one names agents and, as described under
[Tab naming](#tab-naming), the tab they live in.

## Requirements

- herdr `>= 0.7.5` — earlier releases lack the sidebar row tokens
- [`uv`](https://docs.astral.sh/uv/) on your `PATH`
- an OpenAI-compatible API key, **or** the [Claude Code](https://claude.com/claude-code) CLI signed in to a Claude subscription

## Install

```bash
herdr plugin link /path/to/herdr-agent-smart-rename
herdr plugin action invoke configure-ai --plugin chrismalone.agent-smart-rename
herdr plugin action invoke check-ai     --plugin chrismalone.agent-smart-rename
```

`configure-ai` opens `provider.env` in `$VISUAL`/`$EDITOR` inside a herdr overlay
pane — an action's own subprocess has no terminal for an editor to draw on:

```
SMART_RENAME_PROVIDER=openai
OPENAI_API_KEY=sk-...
SMART_RENAME_MODEL=gpt-5.6-luna
SMART_RENAME_BASE_URL=https://api.openai.com/v1
SMART_RENAME_TIMEOUT_MS=20000
```

### Using your Claude subscription instead

Set `SMART_RENAME_PROVIDER=claude` and no API key is needed. Each naming call runs

```
claude -p --model haiku --tools "" --strict-mcp-config --setting-sources "" \
  --disable-slash-commands --no-session-persistence --output-format json \
  --system-prompt <naming-prompt.md>
```

with the evidence on stdin. That gives the call no tools, no MCP servers, no
user/project settings, hooks or plugins, and writes no session. The plugin also
strips `CLAUDE_*` variables (plugin directories, session ids) from the child's
environment, keeping only `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_OAUTH_TOKEN` for
authentication, so a call started from inside a Claude session does not load
that session's plugins.

```
SMART_RENAME_PROVIDER=claude
SMART_RENAME_CLAUDE_MODEL=haiku
SMART_RENAME_CLAUDE_BIN=/Users/you/.local/bin/claude
```

Both of the last two are optional. herdr runs plugin commands with a minimal
environment, so the binary is looked up as `SMART_RENAME_CLAUDE_BIN`, then on
`PATH`, then in `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin` and
`~/.claude/local`. A missing binary is logged to `rename.log` and the pane keeps
its fallback label. `SMART_RENAME_TIMEOUT_MS` bounds the call. Each call spends
subscription quota, about a second of it in CLI startup.

`watch.sh` reads its interval from the environment rather than this file, since it
never loads the provider config: set `SMART_RENAME_POLL_SECONDS` to override 60.

`check-ai` prints what it can see and never spends a model call:

```
provider: model=gpt-5.6-luna base_url=https://api.openai.com/v1 timeout=20000ms
prompt: ok
herdr: socket=ok agents=13
titles: 5/13 panes publish one
named: 0/13 agents currently carry a name
```

With the claude provider the first line instead reads
`provider: claude binary=/Users/you/.local/bin/claude version=2.1.312 model=haiku timeout=20000ms`;
the version comes from `claude --version`, so no model call is made.

## Show the names

The agents panel renders the `agent` token, which is where a name lands. The
default layout already includes it. Adding the terminal title alongside it costs
nothing and materially improves naming quality — herdr only syncs OSC titles
when a config references them:

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "workspace", "tab"],
  ["agent", "terminal_title_stripped"],
]
```

To show the plugin's readable label instead, use the `pane` token, which is
what `herdr pane rename` writes:

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "workspace", "tab"],
  ["pane"],
]
```

Then `herdr server reload-config`. If `check-ai` reports `titles: 0/N`, this is
the missing piece and naming will fall back to the session transcript's titles and your messages.

## Actions

| Action | What it does |
| --- | --- |
| `configure-ai` | edit provider credentials |
| `check-ai` | validate configuration, spending no model call |
| `rename-now` | rename the focused agent immediately, ignoring all skip rules |
| `reset-agent` | clear the focused agent's name and forget it |

Bind the useful ones — plugin manifests cannot declare keybindings, so put them
in your `config.toml`:

```toml
[[keys.command]]
key = "prefix+r"
type = "plugin_action"
command = "chrismalone.agent-smart-rename.rename-now"
description = "rename this agent"
```

## How it decides

Hooks on `pane.agent_detected`, `pane.agent_status_changed` and `pane.exited`
run a short-lived script.

herdr fires those events around the moment an agent starts and then goes quiet —
measured at 7 dispatches in 89 minutes across 10 live agents, every one of them a
pane starting up. A fresh agent has nothing to go on yet, so a plugin that only
answered those events would spend each pane's one chance on an empty screen. That
is why `pane.agent_detected` also starts `watch.sh`, one `sh` loop per pane that
polls every 60 seconds and exits when the agent goes away. It is a watcher, so it
is a process you can see in `ps` — about 2MB each, against roughly 42MB if the
Python script did the waiting itself.

Evidence comes from the agent's OSC terminal title first, because it is free and
already per-agent. Claude publishes a task summary there; codex publishes its
branch name, which is useless, so the plugin adds a Claude session's own titles
and the messages you typed to it, which name the session's overall goal.

An agent is renamed when any signal says the work moved on:

- **Its title changes materially.** Spinner glyphs, elapsed timers and token
  counters are stripped before comparing, so routine ticking triggers nothing.
- **Its context grows by 100k tokens.** A long session drifts onto new work while
  its title stays stale, so the plugin takes a fresh look each 100k. The count is
  read from the Claude session transcript, which is the only place it exists —
  agents that keep no transcript never trip this and rely on the title alone.
- **Claude Code compacts the conversation.** The transcript records a
  `compact_boundary` entry; when it is newer than the last naming, the pane is
  renamed once and the 100k step then measures from the post-compaction size.
  A context that fell to half the naming baseline (and by 50k or more) counts
  too, for when the entry has scrolled out of the transcript tail.

**Until a model label exists, the pane shows the terminal title.** A fresh
agent, a model that abstains, a provider error or a timeout all leave the pane
with no label, so the plugin writes the agent's terminal title there, cut at a
word boundary to fit 26 columns. The title is recorded as plugin-written, so it
is never mistaken for a manual rename. It follows later title changes, and the
first model label replaces it. A label that is already on the pane is never
overwritten by the fallback.

**Rename an agent by hand and the plugin never touches it again.** It notices
that the live name is not the one it wrote and marks the agent as yours.
`rename-now` and `reset-agent` reclaim it.

Names are kebab-case slugs (`review-auth-flow`) because herdr requires
`^[a-z][a-z0-9_-]{0,31}$` — a name doubles as a CLI target, so you can
`herdr agent send review-auth-flow "..."` once one is set. The plugin caps names
at 24 characters so the 26-column sidebar shows them whole.

## Tab naming

Tab auto-titlers such as herdr-bar skip any tab whose pane has a label, so a
labelled agent would leave its tab as a bare number. Whenever the plugin applies
a pane label (model or fallback), it also runs `herdr tab rename` with the same
text, and keeps the two in step when the label changes.

A tab is only renamed when it holds exactly one agent pane and its current name
is not one you chose: empty, its default number (position in the workspace, or
its `number`), what this plugin last wrote, or the agent's current or previous
terminal title. Any other name is treated as manual, recorded, and never touched
again. `reset-agent` renames the tab back (empty, else its position number) only
if it still shows the plugin's name. `check-ai` never touches tabs.

## What gets sent, and where

Naming needs context, so **the sanitised terminal title, the session's titles
(the one you set with `/rename` and Claude Code's own summary), and the first
five and last fifteen messages you typed to the agent (each cut to 200
characters) are sent to your configured provider.** Pane output is never read.
Before anything leaves the machine the payload has `$HOME` rewritten to `~`, and
API keys, GitHub and Slack tokens, bearer tokens, private-key blocks,
`secret=`/`password=`-style assignments, and long high-entropy strings replaced
with `[redacted]`.

That is a real reduction in exposure, not a guarantee. If your messages hold something
that must never reach a third party, don't run this plugin against it.

The titles and your messages come from the Claude session transcript, read from
its start and its end; slash-command echoes, shell output, reminders and tool
results are left out. The transcript is also read for the token count off its
last usage record. Agents that keep no Claude
transcript are named from the terminal title alone.

## Failure behaviour

Every failure path exits 0 and stays quiet — a naming plugin has no business
breaking your multiplexer. No API key, a provider timeout, a junk reply, an
unreachable socket, a corrupt stamp, a concurrent run: all are no-ops.

Every run records why it stopped, because a firing may not come again and a
silent no-op is indistinguishable from a broken plugin. Diagnostics land in the
plugin state dir's `rename.log`, which is appended to and rotated so overlapping
runs cannot lose each other's lines, and in
`herdr plugin log list --plugin chrismalone.agent-smart-rename`.

## Development

```bash
uv run --with pytest --with httpx --with pydantic pytest tests/ -q
```

114 tests over the pure functions and the rename flow — no network, no herdr and no real model call required. Design notes
and the empirically verified platform behaviour are in
`docs/superpowers/specs/2026-07-24-herdr-agent-smart-rename-design.md`.
