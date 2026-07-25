# Agent Smart Rename — herdr plugin design

Date: 2026-07-24
Status: approved, ready to implement

## Problem

herdr's agents panel shows one row per agent. By default each row renders the
workspace name, an optional tab label, and the detected agent label — so a tab
holding five unrelated Claude sessions produces five rows reading `claude`,
distinguishable only by state icon. When several agents share a workspace the
panel cannot tell you which is which.

herdr already has a per-agent name: `herdr agent rename <target> <name>` sets a
per-terminal `agent_name`, and the panel's `agent` sidebar token renders it in
preference to the detected label. Nothing populates it automatically.

## Solution

A herdr plugin that names each agent from what that agent is actually doing, so
the panel's second row reads `Scrape Tweets` rather than `claude`.

## Verified platform facts

Established empirically against the installed herdr 0.7.5 (protocol 17), not
read off a source checkout:

| Fact | Evidence |
| --- | --- |
| `agent rename` exists and takes effect | ran live; API returned `"name":"rename-probe"` |
| Manifest `[[events]]` hooks fire | plugin log `exit_code: 0` |
| Hook learns which pane changed | `HERDR_PLUGIN_EVENT_JSON` → `data.pane_id` |
| Hook can read and write herdr state | `HERDR_SOCKET_PATH` + `HERDR_BIN_PATH` injected; `agent list` returned rc=0 |
| Per-plugin state directory exists | `HERDR_PLUGIN_STATE_DIR` |
| `pane.agent_status_changed` is low volume | one firing observed over several minutes with 11 live agents |
| Claude publishes a usable OSC title | `✳ Scrape and organize tweets from X account` |
| Codex does not | title is the branch name, `main` |
| Sidebar is 26 columns and ellipsizes | `sidebar_width` default 26; rows use `truncate_end` |

Two constraints that follow from 0.7.5's release notes and were designed around:
agent names must be unique across the session, and they are cleared when the
occupant exits, is released, or is replaced.

## Non-goals

- No worker or daemon. Hooks only.
- No `rename-all` sweep across every agent.
- No per-agent configuration.
- No tab renaming — that is `herdr-tab-smart-rename`'s job. This plugin only
  ever writes agent names.

## Architecture

Plugin id `chrismalone.agent-smart-rename`. Six files, no long-lived process:

```
herdr-plugin.toml      hooks + actions
rename.py              uv PEP 723 script; the whole engine
naming-prompt.md       user-editable prompt, copied to the config dir on first run
provider.env.example   documents the provider contract
README.md
tests/test_rename.py   pytest over the pure functions
```

### Trigger

```
pane.agent_detected ───────┐
pane.agent_status_changed ─┼──> rename.py
pane.exited ───────────────┘
```

`pane.agent_detected` fires before the agent has done anything, so there is
usually no task to name yet. Rather than special-casing that event, the script
exits quietly whenever the evidence is too thin and tries again on the next
firing. This is self-correcting and needs no extra state.

`pane.exited` runs the stamp-cleanup path only.

### Flow

```
read pane_id from event JSON
  └─ pane.exited?               -> delete stamp, exit 0
load stamp for pane
  ├─ stamp marked manual?       -> exit 0
  ├─ agent_session_id changed?  -> discard stamp (new agent, same pane)
  ├─ live name != name we set?  -> mark manual, exit 0
  ├─ named and title unchanged? -> exit 0
  └─ renamed within floor?      -> exit 0
acquire per-pane lock (O_EXCL)  -> busy? exit 0
gather evidence -> too thin?    -> exit 0
sanitise
call model
validate name -> invalid?       -> exit 0
herdr agent rename              -> name taken? disambiguate and retry
write stamp
```

Every early exit is status 0. A naming plugin must never break the
multiplexer, and must never spam its log.

### Actions

| Action | Purpose |
| --- | --- |
| `configure-ai` | open `provider.env` in `$EDITOR` |
| `check-ai` | validate config and reachability without spending a model call |
| `rename-now` | force a rename of the focused agent, ignoring the stamp |
| `reset-agent` | clear the focused agent's name and delete its stamp |

`rename-now` clears a manual mark: an explicit action reclaims its target.

## Naming policy

### Evidence, in priority order

1. **OSC title** — `terminal_title_stripped` from `herdr pane get`. Free, already
   per-agent, and for Claude it is a task summary.
2. **Recent pane output** — `herdr agent read <pane> --source recent --format text
   --lines 120`, used when the title is missing or junk. `--format text` is
   requested so no ANSI ever reaches the sanitiser.
3. **cwd basename and git branch** — supporting context only, never the whole name.

A title counts as junk when it is empty, equals the cwd basename, equals the git
branch, or is a bare path. That is what demotes codex's `main` to route 2.

Evidence is "too thin" when there is no usable title *and* the recent output is
below a minimum length or matches only a known agent banner.

### Sanitisation, before anything leaves the machine

Pane content goes to a third-party API, so this is applied to every payload:

- `$HOME` rewritten to `~`
- secret-shaped tokens redacted: `sk-…`, `ghp_…`, `github_pat_…`, `AKIA…`,
  `xoxb-/xoxp-/xoxa-/xoxr-…`, `Bearer <token>`, `-----BEGIN … KEY-----` blocks,
  and `key=value` / `key: value` where the key contains `token`, `secret`,
  `password`, `passwd`, `api_key`, or `apikey`
- long unbroken high-entropy runs (≥32 chars of base64/hex alphabet) redacted
- payload capped at 4000 characters, keeping the tail (most recent output)

Redaction replaces the value with `[redacted]`, never the surrounding text, so
the model still sees the shape of the work.

### Output constraints

- 2 to 4 words
- at most 24 characters — the sidebar is 26 columns and ellipsizes
- Title Case, acronyms preserved
- describes the task, not the tool or the model: `Scrape Tweets`, never
  `Claude Session`
- single line, no punctuation beyond internal hyphens

A name failing any constraint is discarded rather than written. The prompt lives
in `naming-prompt.md`, copied into the plugin config dir on first run so it can
be tuned without editing code.

## Provider contract

OpenAI-compatible chat completions. Mirrors `herdr-tab-smart-rename`'s contract
so one provider setup serves both plugins.

`~/.config/herdr/plugins/config/chrismalone.agent-smart-rename/provider.env`:

```
OPENAI_API_KEY=sk-...
SMART_RENAME_MODEL=gpt-5.6-luna
SMART_RENAME_BASE_URL=https://api.openai.com/v1
SMART_RENAME_TIMEOUT_MS=20000
```

Process environment overrides the file. The response is parsed tolerantly: the
first JSON object in the message content is extracted and validated against a
Pydantic model, so the plugin works against providers with and without native
JSON-mode support. A response that yields no valid object is discarded.

## State

One stamp per pane at `$HERDR_PLUGIN_STATE_DIR/panes/<pane_id>.json`:

```json
{
  "name_set": "Scrape Tweets",
  "title_hash": "9f2c…",
  "agent_session_id": "66087717-d7aa-4ced-a43e-850d3a1cc51f",
  "set_at": 1784937769,
  "manual": false
}
```

Pane ids contain a colon (`w15:p1`), which is legal in a filename on both target
platforms; it is replaced with `-` anyway to keep the directory tidy.

- **Manual names win permanently.** If the live `agent.name` differs from
  `name_set` and is non-empty, the user renamed it by hand. Set `manual: true`
  and never touch that pane again unless `rename-now` or `reset-agent` is invoked.
- **Re-name only on material change.** `title_hash` is computed over a normalised
  title with spinner glyphs, elapsed-time counters, and token counts stripped, so
  routine ticking does not trigger a rename.
- **New agent in the same pane.** A stamp whose `agent_session_id` no longer
  matches the live agent session is discarded, so the replacement gets its own name.
- **Rename floor.** At most one rename per pane per 60 seconds, enforced via `set_at`.
- **Cleanup.** `pane.exited` deletes the stamp. Stamps for panes absent from
  `agent list` are pruned opportunistically on each run.

### Concurrency

Several agents can transition at once, and each firing is its own process. A
per-pane lock file created with `O_EXCL` prevents two firings racing to name the
same agent; a lock older than 120 seconds is treated as stale and broken. A busy
lock is not an error — the run exits 0 and the next firing picks it up.

### Collisions

Agent names must be unique session-wide. On `agent_name_taken`, retry once with
the workspace label as a prefix (`pipette Scrape Tweets`), then with a numeric
suffix (`Scrape Tweets 2`), truncating to stay within 24 characters. If all
attempts collide, exit 0 without renaming.

## Failure handling

| Failure | Behaviour |
| --- | --- |
| No `provider.env`, or no API key | log once per day, exit 0 |
| Model error, timeout, or non-2xx | exit 0; next firing retries |
| Model returns junk or violates constraints | discard, exit 0 |
| `herdr` CLI missing or socket unreachable | exit 0 |
| Stamp file corrupt | treat as absent |
| Lock held by another run | exit 0 |

Diagnostics go to `$HERDR_PLUGIN_STATE_DIR/rename.log`, capped by truncating to
the most recent 200 lines on each run, and are visible via
`herdr plugin log list --plugin chrismalone.agent-smart-rename`.

## Testing

`pytest` over the pure functions, no network and no herdr required:

- title junk classification, including codex's branch-name case
- evidence selection across the three routes and the too-thin case
- sanitisation: each secret pattern, home-path rewriting, cap behaviour
- title normalisation and hash stability across spinner and timer ticks
- name validation: word count, length, casing, rejection cases
- collision disambiguation and its 24-character truncation
- the stamp state machine: fresh, unchanged, materially changed, manual,
  replaced session, within floor

A manual smoke test through `check-ai` and `rename-now` covers the wiring that
unit tests cannot: real hook firing, real socket write, real panel render.

## Build order

1. `herdr-plugin.toml` and the repo skeleton
2. pure functions in `rename.py` with tests alongside
3. herdr I/O and the model call
4. actions
5. link and smoke test
6. README
