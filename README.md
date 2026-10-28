# herdr-plugins

Plugins for [Herdr](https://herdr.dev) 0.9+. Each folder is a self-contained plugin with its own README.

| Plugin | What it does | Install |
|---|---|---|
| [new-tab](new-tab/) | Create New Tab popup that can put the tab in any workspace | `herdr plugin install danielannan17/herdr-plugins/new-tab` |
| [dashboard](dashboard/) | Popup that runs a personal or per-workspace terminal dashboard (`gh dash`, `gh board`, …) | `herdr plugin install danielannan17/herdr-plugins/dashboard` |

Both need `python3` (standard library only). Run a plugin's tests from its folder:

```bash
python3 -m unittest discover -s tests -t .
```
