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
- an OpenAI-compatible API key

## Install

```bash
herdr plugin link /path/to/herdr-agent-smart-rename
herdr plugin action invoke configure-ai --plugin chrismalone.agent-smart-rename
herdr plugin action invoke check-ai     --plugin chrismalone.agent-smart-rename
```

`configure-ai` opens `provider.env` in `$EDITOR`:

```
OPENAI_API_KEY=sk-...
SMART_RENAME_MODEL=gpt-5.6-luna
SMART_RENAME_BASE_URL=https://api.openai.com/v1
SMART_RENAME_TIMEOUT_MS=20000
```

`check-ai` prints what it can see and never spends a model call:

```
provider: model=gpt-5.6-luna base_url=https://api.openai.com/v1 timeout=20000ms
prompt: ok
herdr: socket=ok agents=13
titles: 5/13 panes publish one
named: 0/13 agents currently carry a name
```

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
run a short-lived script — there is no daemon to start, supervise, or restart.

Evidence comes from the agent's OSC terminal title first, because it is free and
already per-agent. Claude publishes a task summary there; codex publishes its
branch name, which is useless, so the plugin falls back to reading recent pane
output. A fresh agent with neither is left alone until it has done something.

An agent is named once and then renamed only when its title changes materially —
spinner glyphs, elapsed timers, and token counters are stripped before comparing,
so routine ticking triggers nothing.

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

## Failure behaviour

Every failure path exits 0 and stays quiet — a naming plugin has no business
breaking your multiplexer. No API key, a provider timeout, a junk reply, an
unreachable socket, a corrupt stamp, a concurrent run: all are no-ops that
retry on the next firing. Diagnostics land in the plugin state dir's
`rename.log` and in `herdr plugin log list --plugin chrismalone.agent-smart-rename`.

## Development

```bash
uv run --with pytest --with httpx --with pydantic pytest tests/ -q
```

44 tests over the pure functions — no network, no herdr required. Design notes
and the empirically verified platform behaviour are in
`docs/superpowers/specs/2026-07-24-herdr-agent-smart-rename-design.md`.
