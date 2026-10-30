# herdr-plugins

Plugins for [Herdr](https://herdr.dev) 0.9+. Each folder is a self-contained plugin with its own README.

| Plugin | What it does | Install |
|---|---|---|
| [new-tab](new-tab/) | Create New Tab popup that can put the tab in any workspace | `herdr plugin install danielannan17/herdr-plugins/new-tab` |
| [agent-smart-rename](agent-smart-rename/) | Names each agent pane after the work it is doing (forked from malone-c/herdr-agent-smart-rename) | `herdr plugin install danielannan17/herdr-plugins/agent-smart-rename` |
| [dashboard](dashboard/) | Popup that runs a personal or per-workspace terminal dashboard (`gh dash`, `gh board`, …) | `herdr plugin install danielannan17/herdr-plugins/dashboard` |
| [herdr-notes](herdr-notes/) | A markdown notes pane with a note per workspace and a global note, switched by tabs (forked from alexarthurs/herdr-notes) | `herdr plugin install danielannan17/herdr-plugins/herdr-notes` |
| [herdr-easyjump](herdr-easyjump/) | Hint-label jumping to any space, agent, pane or tab (forked from xzedx/herdr-easyjump) | `herdr plugin install danielannan17/herdr-plugins/herdr-easyjump` |

new-tab and dashboard need only `python3`; herdr-notes and herdr-easyjump are Rust crates built with `cargo`; agent-smart-rename needs `uv` (see its README for its tests). Run new-tab's or dashboard's tests from its folder:

```bash
python3 -m unittest discover -s tests -t .
```

## Other plugins I use

Plugins by other people that I run alongside these.

| Plugin | What it does | Install |
|---|---|---|
| [herdr-session-parker](https://github.com/iviaxpow3r/herdr-session-parker) | Parks a pane or tab, closing its agent to free resources, and resumes the agent session later | `herdr plugin install iviaxpow3r/herdr-session-parker` |
