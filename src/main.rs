//! herdr-easyjump: Vimium-style hint jump for Herdr.
//!
//! Two modes, both drawn inside Herdr's own terminal popup so the outer
//! terminal never loses focus:
//!
//!   sidebar (default)  Hint labels appear where the eye already is: next to
//!                      every space row and agent row in the sidebar (metadata
//!                      tokens), on the border of every pane of the current tab
//!                      (title override), and in the tab bar (a label prefix on
//!                      custom-named tabs; auto-numbered tabs use their number).
//!                      A tiny HUD popup captures the key and repeats the pane
//!                      and tab hints. When the sidebar is collapsed the HUD
//!                      lists the spaces and agents too.
//!   list               One big popup with a pane mini-map and full lists.
//!
//! Usage:
//!   herdr-easyjump                 sidebar-mode HUD (manifest pane "jump")
//!   herdr-easyjump --list          list-mode popup (manifest pane "list")
//!   herdr-easyjump --open          open the sidebar-mode popup, sized to fit
//!   herdr-easyjump --open-list     open the list-mode popup
//!   herdr-easyjump --back          jump back to the pane focused before the last hop
//!   herdr-easyjump --clear         clear every hint token (detached cleanup helper)
//!   herdr-easyjump --reopen[-list] reopen the popup after a space/tab hop (detached helper)
//!   herdr-easyjump --dump [--sidebar]   render one frame to stdout (debugging)
//!   herdr-easyjump --probe         write the popup size to the state dir (debugging)

mod model;
mod render;
mod rpc;
mod state;
mod term;

use model::{Context, Kind, Mode, Model, Snapshot, ALPHABET, REFRESH_MS};
use rpc::Client;
use serde_json::json;
use state::trace;
use std::sync::atomic::Ordering;
use term::Key;

fn plugin_id() -> String {
    std::env::var("HERDR_PLUGIN_ID")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "xzedx.easyjump".into())
}

fn load_model(client: &mut Client, mode: Mode, ctx: Context) -> Result<Model, String> {
    let snap: Snapshot =
        serde_json::from_value(client.snapshot()?).map_err(|e| format!("snapshot: {e}"))?;
    Ok(Model::new(snap, ctx, mode))
}

/// After a relative move: re-read the session so the HUD and labels follow
/// the new focus, and refresh the sidebar tokens.
fn reload(model: &mut Model, client: &mut Client, mode: Mode, typed: &str) {
    if let Ok(m) = load_model(client, mode, Context::default()) {
        *model = m;
    }
    if mode == Mode::Sidebar {
        model.publish_hints(client, typed);
    }
}

enum Outcome {
    Jump(model::Dest),
    Back,
    /// J/K/H/L: focus another space or tab, then reopen the popup there.
    ///
    /// Since Herdr 0.9 a popup belongs to the tab it was opened on and is only
    /// drawn to clients viewing that tab, so switching the space or tab from
    /// inside the popup makes it vanish (the process keeps running, unseen).
    /// The only way to follow the focus is to close and reopen the popup.
    Hop {
        method: &'static str,
        key: &'static str,
        id: String,
    },
    /// Enter: keep whatever focus the relative moves produced.
    Confirm,
    /// Esc / q: undo relative moves, back to where the popup opened.
    Cancel,
    /// Herdr closed the popup from outside; keep the focus as it is.
    Quit,
}

/// The interactive loop. Returns what to do; the caller handles cleanup.
fn interact(model: &mut Model, client: &mut Client, mode: Mode, typed: &mut String) -> Outcome {
    let sidebar = mode == Mode::Sidebar;
    // EASYJUMP_TEST_KEYS=<chars> feeds key presses before reading the terminal (testing).
    let mut test_keys: std::collections::VecDeque<char> = std::env::var("EASYJUMP_TEST_KEYS")
        .map(|s| s.chars().collect())
        .unwrap_or_default();
    if sidebar {
        model.publish_hints(client, typed);
        trace("popup:hints");
    }
    loop {
        if sidebar {
            let (cols, _) = term::size();
            term::draw(&render::hud_lines(model, typed, cols));
        } else {
            let (cols, rows) = term::size();
            term::draw(&render::render_list_frame(model, typed, cols, rows));
        }
        trace("popup:drawn");
        if term::INTERRUPTED.load(Ordering::SeqCst) {
            return Outcome::Quit;
        }
        let key = match test_keys.pop_front().map(Key::Char) {
            Some(k) => k,
            None => match term::read_key_timeout(REFRESH_MS) {
                Some(k) => k,
                None => {
                    // Idle: refresh the tokens so their TTL never lapses while open.
                    if sidebar {
                        model.publish_hints(client, typed);
                    }
                    continue;
                }
            },
        };
        trace(&format!("popup:key:{key:?}"));
        match key {
            Key::Eof => {
                // Herdr closed the popup from outside and will kill us within a
                // millisecond or two: hand the cleanup to a detached helper.
                if sidebar {
                    spawn_clear_helper();
                }
                return Outcome::Quit;
            }
            Key::Esc | Key::CtrlC | Key::Char('q') => return Outcome::Cancel,
            Key::Seq => {}
            Key::Backspace => {
                typed.pop();
                if sidebar {
                    model.publish_hints(client, typed);
                }
            }
            Key::Char('`') | Key::Char('\'') => return Outcome::Back,
            // Relative movement, Vim style: panes with hjkl, tabs with H/L, spaces with J/K.
            Key::Char(c @ ('h' | 'j' | 'k' | 'l')) => {
                let dir = match c {
                    'h' => "left",
                    'j' => "down",
                    'k' => "up",
                    _ => "right",
                };
                let _ = client.call("pane.focus_direction", json!({ "direction": dir }));
                reload(model, client, mode, typed);
            }
            // Spaces with J/K and tabs with H/L leave the current tab, which
            // hides the popup: hand over to a fresh popup on the target.
            Key::Char(c @ ('J' | 'K')) => {
                let step = if c == 'J' { 1 } else { -1 };
                if let Some(id) = model.neighbour_workspace(step) {
                    return Outcome::Hop {
                        method: "workspace.focus",
                        key: "workspace_id",
                        id,
                    };
                }
            }
            Key::Char(c @ ('H' | 'L')) => {
                let step = if c == 'L' { 1 } else { -1 };
                if let Some(id) = model.neighbour_tab(step) {
                    return Outcome::Hop {
                        method: "tab.focus",
                        key: "tab_id",
                        id,
                    };
                }
            }
            Key::Enter => {
                let matches = model.labels_with_prefix(typed);
                if !typed.is_empty() && matches.len() == 1 {
                    if let Some(d) = model.dest_for(matches[0]) {
                        return Outcome::Jump(d.clone());
                    }
                }
                return Outcome::Confirm;
            }
            // Letters are labels; digits are the keys of auto-numbered tabs.
            Key::Char(c) if ALPHABET.contains(c.to_ascii_lowercase()) || c.is_ascii_digit() => {
                typed.push(c.to_ascii_lowercase());
                if let Some(d) = model.dest_for(typed) {
                    return Outcome::Jump(d.clone());
                }
                if model.labels_with_prefix(typed).is_empty() {
                    typed.clear();
                }
                if sidebar {
                    model.publish_hints(client, typed);
                }
            }
            Key::Char(_) => {}
        }
    }
}

/// Start `herdr-easyjump --clear` in its own process group so it survives us.
fn spawn_clear_helper() {
    use std::os::unix::process::CommandExt;
    if let Ok(exe) = std::env::current_exe() {
        let _ = std::process::Command::new(exe)
            .arg("--clear")
            .process_group(0)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
    }
}

/// Start `herdr-easyjump --reopen[-list]` in its own process group. It waits
/// for this popup to be gone, then opens a new one on the tab that now has
/// the focus, carrying the typed prefix and the original pane along.
fn spawn_reopen_helper(mode: Mode, typed: &str, origin: Option<&str>) {
    use std::os::unix::process::CommandExt;
    if let Ok(exe) = std::env::current_exe() {
        let flag = match mode {
            Mode::Sidebar => "--reopen",
            Mode::List => "--reopen-list",
        };
        let _ = std::process::Command::new(exe)
            .arg(flag)
            .env("EASYJUMP_TYPED", typed)
            .env("EASYJUMP_ORIGIN", origin.unwrap_or(""))
            .env_remove("EASYJUMP_TEST_KEYS")
            .process_group(0)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
    }
}

/// Detached half of a hop: retry `plugin.pane.open` until the old popup has
/// been torn down (Herdr allows one popup at a time), for at most a second.
fn reopen_popup(mode: Mode) -> Result<(), String> {
    let typed = std::env::var("EASYJUMP_TYPED").unwrap_or_default();
    let origin = std::env::var("EASYJUMP_ORIGIN").unwrap_or_default();
    let deadline = std::time::Instant::now() + std::time::Duration::from_millis(1000);
    loop {
        match open_popup(mode, Context::default(), Some((&typed, &origin))) {
            Ok(()) => {
                trace("reopen:ok");
                return Ok(());
            }
            Err(e) if std::time::Instant::now() < deadline => {
                trace(&format!("reopen:retry:{e}"));
                std::thread::sleep(std::time::Duration::from_millis(3));
            }
            Err(e) => return Err(format!("reopen: {e}")),
        }
    }
}

fn clear_tokens() -> Result<(), String> {
    let mut client = Client::connect()?;
    let snap: Snapshot = serde_json::from_value(client.snapshot()?).map_err(|e| e.to_string())?;
    Model::clear_all(&mut client, &snap);
    trace("clear:done");
    Ok(())
}

fn run_popup(mode: Mode) -> Result<(), String> {
    trace("popup:start");
    let mut client = Client::connect()?;
    let mut model = load_model(&mut client, mode, Context::from_env())?;
    // After a hop the previous popup hands over where it started and what
    // had been typed, so Esc and jump-back still mean the first origin.
    let origin = std::env::var("EASYJUMP_ORIGIN")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| model.focused_pane.clone());
    let mut typed = std::env::var("EASYJUMP_TYPED").unwrap_or_default();
    trace(&format!("popup:model origin={origin:?} typed={typed:?}"));
    term::install_signal_handlers();
    let raw = term::RawMode::enable();
    term::enter_alt_screen();

    let outcome = interact(&mut model, &mut client, mode, &mut typed);
    trace("popup:quit");

    if mode == Mode::Sidebar {
        model.clear_hints(&mut client);
        trace("popup:cleared");
    }
    drop(raw);
    term::leave_alt_screen();

    if let Outcome::Hop { method, key, id } = &outcome {
        trace(&format!("popup:hop {method} {id}"));
        client.call(method, json!({ *key: id }))?;
        spawn_reopen_helper(mode, &typed, origin.as_deref());
        return Ok(());
    }

    // "Previous" always means where we were when the popup opened, even
    // after a chain of relative moves. A jump-back or a cancel must not
    // overwrite it.
    let moved = model.focused_pane != origin;
    let remember = match &outcome {
        Outcome::Jump(_) => true,
        Outcome::Confirm | Outcome::Quit => moved,
        Outcome::Back | Outcome::Cancel | Outcome::Hop { .. } => false,
    };
    if remember {
        state::remember_previous(origin.as_deref());
    }
    match outcome {
        Outcome::Jump(dest) => model.jump(&mut client, &dest),
        Outcome::Back => match state::read_previous() {
            Some(prev) if model.panes.contains_key(&prev) => {
                model.jump(&mut client, &(Kind::Pane, prev))
            }
            _ => Ok(()),
        },
        Outcome::Cancel => match origin {
            Some(o) if moved && model.panes.contains_key(&o) => {
                model.jump(&mut client, &(Kind::Pane, o))
            }
            _ => Ok(()),
        },
        Outcome::Confirm | Outcome::Quit | Outcome::Hop { .. } => Ok(()),
    }
}

/// Ask Herdr to open the popup, sized to the HUD's content. `handoff` is the
/// (typed prefix, origin pane) a hop carries over into the new popup's env.
fn open_popup(mode: Mode, ctx: Context, handoff: Option<(&str, &str)>) -> Result<(), String> {
    let mut client = Client::connect()?;
    let mut params = json!({ "plugin_id": plugin_id(), "placement": "popup" });
    if let Some((typed, origin)) = handoff {
        params["env"] = json!({ "EASYJUMP_TYPED": typed, "EASYJUMP_ORIGIN": origin });
        // Let a trace file follow the hop into the new popup.
        if let Ok(t) = std::env::var("EASYJUMP_TRACE") {
            params["env"]["EASYJUMP_TRACE"] = json!(t);
        }
    }
    match mode {
        Mode::Sidebar => {
            let model = load_model(&mut client, Mode::Sidebar, ctx)?;
            // Fit the content, but stay inside the outer terminal when known.
            let max_w = if model.screen_cols > 12 {
                (model.screen_cols - 8).min(120)
            } else {
                120
            };
            let max_h = if model.screen_rows > 6 {
                model.screen_rows - 4
            } else {
                usize::MAX
            };
            let lines = render::hud_lines(&model, "", max_w - 4);
            let width = lines
                .iter()
                .map(|l| render::visible_len(l))
                .max()
                .unwrap_or(40)
                + 4;
            params["entrypoint"] = json!("jump");
            params["width"] = json!(width.min(max_w));
            params["height"] = json!((lines.len() + 2).min(max_h));
        }
        Mode::List => params["entrypoint"] = json!("list"),
    }
    client.call("plugin.pane.open", params).map(|_| ())
}

fn jump_back() -> Result<(), String> {
    let Some(prev) = state::read_previous() else {
        return Err("nothing to jump back to".into());
    };
    let mut client = Client::connect()?;
    let snap: Snapshot = serde_json::from_value(client.snapshot()?).map_err(|e| e.to_string())?;
    if !snap.panes.iter().any(|p| p.pane_id == prev) {
        return Err(format!("previous pane {prev} is gone"));
    }
    state::remember_previous(snap.focused_pane_id.as_deref());
    client
        .call("pane.focus", json!({ "pane_id": prev }))
        .map(|_| ())
}

fn dump(args: &[String]) -> Result<(), String> {
    let mode = if args.iter().any(|a| a == "--sidebar") {
        Mode::Sidebar
    } else {
        Mode::List
    };
    let mut client = Client::connect()?;
    let model = load_model(&mut client, mode, Context::from_env())?;
    let env = |k: &str, d: usize| {
        std::env::var(k)
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(d)
    };
    let typed = std::env::var("EASYJUMP_TYPED").unwrap_or_default();
    let lines = match mode {
        Mode::Sidebar => render::hud_lines(&model, &typed, env("COLUMNS", 100)),
        Mode::List => {
            render::render_list_frame(&model, &typed, env("COLUMNS", 160), env("LINES", 30))
        }
    };
    println!("{}{}", lines.join("\n"), render::RESET);
    println!(
        "sidebar: {} cols ({}), screen: {}x{}",
        model.sidebar_cols,
        if model.sidebar_collapsed() {
            "collapsed"
        } else {
            "expanded"
        },
        model.screen_cols,
        model.screen_rows
    );
    let map: Vec<String> = model
        .labels
        .iter()
        .zip(&model.dests)
        .map(|(l, (k, id))| format!("{l}:{k:?}:{id}"))
        .collect();
    println!("labels: {}", map.join(" "));
    Ok(())
}

fn main() {
    trace("main:start");
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    if let Ok(extra) = std::env::var("EASYJUMP_ARGS") {
        args.extend(extra.split_whitespace().map(String::from));
    }
    let has = |flag: &str| args.iter().any(|a| a == flag);

    let result = if has("--open") {
        open_popup(Mode::Sidebar, Context::from_env(), None)
    } else if has("--open-list") {
        open_popup(Mode::List, Context::from_env(), None)
    } else if has("--reopen") {
        reopen_popup(Mode::Sidebar)
    } else if has("--reopen-list") {
        reopen_popup(Mode::List)
    } else if has("--back") {
        jump_back()
    } else if has("--clear") {
        clear_tokens()
    } else if has("--dump") {
        dump(&args)
    } else if has("--probe") {
        let (c, r) = term::size();
        std::fs::write(
            state::state_dir().join("probe.txt"),
            format!("size=({c}, {r})\n"),
        )
        .map_err(|e| e.to_string())
    } else {
        let mode = if has("--list") {
            Mode::List
        } else {
            Mode::Sidebar
        };
        run_popup(mode)
    };

    if let Err(e) = result {
        term::leave_alt_screen();
        eprintln!("easyjump: {e}");
        std::process::exit(1);
    }
}
