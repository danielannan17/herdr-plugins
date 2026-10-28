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
names *tabs*. This one only ever writes *agent* names, so the two can run together.

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
the missing piece and naming will fall back to reading pane output.

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
branch name, which is useless, so the plugin falls back to reading recent pane
output.

An agent is renamed when either signal says the work moved on:

- **Its title changes materially.** Spinner glyphs, elapsed timers and token
  counters are stripped before comparing, so routine ticking triggers nothing.
- **Its context grows by 100k tokens.** A long session drifts onto new work while
  its title stays stale, so the plugin takes a fresh look each 100k. The count is
  read from the Claude session transcript, which is the only place it exists —
  agents that keep no transcript never trip this and rely on the title alone.

**Until a model label exists, the pane shows the terminal title.** A fresh
agent, a model that abstains, a provider error or a timeout all leave the pane
with no label, so the plugin writes the agent's terminal title there, cut at a
word boundary to fit 26 columns. The title is recorded as plugin-written, so it
is never mistaken for a manual rename. It follows later title changes, and the
first model label replaces it. A label that is already on the pane is never
overwritten by the fallback.

When the pane output comes from Claude Code, the input box and everything
rendered below it (the statusline, the mode line) is dropped before the output
is sent. The cut is made at the rule above the last `❯` prompt line; output with
no rule above its last prompt is sent whole.

**Rename an agent by hand and the plugin never touches it again.** It notices
that the live name is not the one it wrote and marks the agent as yours.
`rename-now` and `reset-agent` reclaim it.

Names are kebab-case slugs (`review-auth-flow`) because herdr requires
`^[a-z][a-z0-9_-]{0,31}$` — a name doubles as a CLI target, so you can
`herdr agent send review-auth-flow "..."` once one is set. The plugin caps names
at 24 characters so the 26-column sidebar shows them whole.

## What gets sent, and where

Naming needs context, so **the sanitised terminal title and a tail of recent
pane output are sent to your configured provider.** Before anything leaves the
machine the payload has `$HOME` rewritten to `~`, and API keys, GitHub and Slack
tokens, bearer tokens, private-key blocks, `secret=`/`password=`-style
assignments, and long high-entropy strings replaced with `[redacted]`.

That is a real reduction in exposure, not a guarantee. If a pane holds something
that must never reach a third party, don't run this plugin against it.

The Claude session transcript is opened only to read the token count off its last
usage record. Nothing from it is sent anywhere.

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

97 tests over the pure functions and the rename flow — no network, no herdr and no real model call required. Design notes
and the empirically verified platform behaviour are in
`docs/superpowers/specs/2026-07-24-herdr-agent-smart-rename-design.md`.
