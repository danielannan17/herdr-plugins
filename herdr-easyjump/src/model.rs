//! Session snapshot types and the label model.

use crate::agent_sort::{self, AgentSort};
use crate::rpc::Client;
use serde::Deserialize;
use serde_json::{json, Value};
use std::cmp::Reverse;
use std::collections::{HashMap, HashSet};

/// Home-row first. "q" is reserved for quitting, so it never appears in a label.
pub const ALPHABET: &str = "asdfghjklwertyuiopzxcvbnm";
/// Keys of the first ten agents, in sidebar order.
pub const AGENT_DIGITS: &str = "1234567890";
pub const TOKEN: &str = "hint"; // sidebar rows render this as $hint
pub const SOURCE: &str = "xzedx.easyjump";
pub const TOKEN_TTL_MS: u64 = 15_000; // backstop: labels vanish on their own if we die
pub const REFRESH_MS: i32 = 5_000; // re-publish while open so the TTL never expires under us
/// Herdr does not report whether the sidebar is open. The pane area of every
/// tab starts to the right of it, so its x offset is the sidebar's width: about
/// 30 columns when expanded, 4 when collapsed to the icon rail, 0 when hidden.
/// Anything narrower than this cannot show a `[xx]` label next to a name.
pub const SIDEBAR_MIN_COLS: i64 = 12;

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Rect {
    pub x: i64,
    pub y: i64,
    pub width: i64,
    pub height: i64,
}

#[derive(Deserialize, Clone, Debug)]
pub struct LayoutPane {
    pub pane_id: String,
    pub rect: Rect,
}

#[derive(Deserialize, Clone, Debug)]
pub struct Layout {
    pub tab_id: String,
    #[serde(default)]
    pub area: Rect,
    #[serde(default)]
    pub panes: Vec<LayoutPane>,
}

#[derive(Deserialize, Clone, Default, Debug)]
pub struct Workspace {
    pub workspace_id: String,
    #[serde(default)]
    pub label: String,
    #[serde(default)]
    pub number: i64,
}

#[derive(Deserialize, Clone, Default, Debug)]
pub struct Tab {
    pub tab_id: String,
    pub workspace_id: String,
    #[serde(default)]
    pub label: String,
    #[serde(default)]
    pub number: i64,
    #[serde(default)]
    pub pane_count: i64,
    #[serde(default)]
    pub agent_status: Option<String>,
}

#[derive(Deserialize, Clone, Default, Debug)]
pub struct Pane {
    pub pane_id: String,
    #[serde(default)]
    pub tab_id: String,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub terminal_title_stripped: Option<String>,
}

#[derive(Deserialize, Clone, Default, Debug)]
pub struct Agent {
    pub pane_id: String,
    #[serde(default)]
    pub workspace_id: String,
    #[serde(default)]
    pub agent: Option<String>,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub display_agent: Option<String>,
    #[serde(default)]
    pub agent_status: Option<String>,
    #[serde(default)]
    pub state_change_seq: u64,
    #[serde(default)]
    pub terminal_title_stripped: Option<String>,
    #[serde(default)]
    pub cwd: Option<String>,
}

#[derive(Deserialize, Default, Debug)]
pub struct Snapshot {
    #[serde(default)]
    pub focused_pane_id: Option<String>,
    #[serde(default)]
    pub focused_tab_id: Option<String>,
    #[serde(default)]
    pub focused_workspace_id: Option<String>,
    #[serde(default)]
    pub workspaces: Vec<Workspace>,
    #[serde(default)]
    pub tabs: Vec<Tab>,
    #[serde(default)]
    pub panes: Vec<Pane>,
    #[serde(default)]
    pub agents: Vec<Agent>,
    #[serde(default)]
    pub layouts: Vec<Layout>,
}

/// The bits of HERDR_PLUGIN_CONTEXT_JSON we care about.
#[derive(Deserialize, Default, Debug)]
pub struct Context {
    #[serde(default)]
    pub workspace_id: Option<String>,
    #[serde(default)]
    pub tab_id: Option<String>,
}

impl Context {
    pub fn from_env() -> Self {
        std::env::var("HERDR_PLUGIN_CONTEXT_JSON")
            .ok()
            .and_then(|raw| serde_json::from_str(&raw).ok())
            .unwrap_or_default()
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Mode {
    Sidebar,
    List,
}

#[derive(Clone, PartialEq, Eq, Hash, Debug)]
pub enum Kind {
    Pane,
    Tab,
    Workspace,
}

pub type Dest = (Kind, String);

/// What a destination is, as far as label assignment cares.
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum Role {
    Agent,
    /// A space, with its name (its letters pick the label).
    Space(String),
    Pane,
    Tab,
}

fn two_letter_labels(n: usize) -> Vec<String> {
    let letters: Vec<char> = ALPHABET.chars().collect();
    let mut out = Vec::with_capacity(n);
    for a in &letters {
        for b in &letters {
            if out.len() == n {
                return out;
            }
            out.push(format!("{a}{b}"));
        }
    }
    out
}

/// One label per role, in the same order. Agents take the digits 1-9, 0 in
/// order. Spaces take a letter of their own name, then tabs take what the
/// spaces left, then panes take what the tabs left, then agents beyond the
/// tenth take what is left of that, so splitting a pane never moves a tab's
/// letter. When the letters cannot cover everything, every letter label becomes
/// two letters (digits stay single, so no label is a prefix of another).
pub fn assign_labels(roles: &[Role]) -> Vec<String> {
    let mut labels = vec![String::new(); roles.len()];
    let mut spaces = Vec::new();
    let mut tabs = Vec::new();
    let mut panes = Vec::new();
    let mut agents = Vec::new();
    for (i, role) in roles.iter().enumerate() {
        match role {
            Role::Space(_) => spaces.push(i),
            Role::Tab => tabs.push(i),
            Role::Pane => panes.push(i),
            Role::Agent => agents.push(i),
        }
    }
    let overflow = agents.split_off(agents.len().min(AGENT_DIGITS.len()));
    for (&i, digit) in agents.iter().zip(AGENT_DIGITS.chars()) {
        labels[i] = digit.to_string();
    }

    let mut free: Vec<char> = ALPHABET.chars().collect();
    let lettered: Vec<usize> = spaces
        .iter()
        .chain(&tabs)
        .chain(&panes)
        .chain(&overflow)
        .copied()
        .collect();
    if lettered.len() > free.len() {
        for (i, label) in lettered.into_iter().zip(two_letter_labels(roles.len())) {
            labels[i] = label;
        }
        return labels;
    }
    for &i in &spaces {
        let Role::Space(name) = &roles[i] else {
            continue;
        };
        let own = name
            .chars()
            .map(|c| c.to_ascii_lowercase())
            .find_map(|c| free.iter().position(|&f| f == c));
        labels[i] = free.remove(own.unwrap_or(0)).to_string();
    }
    for (&i, letter) in tabs.iter().chain(&panes).chain(&overflow).zip(free) {
        labels[i] = letter.to_string();
    }
    labels
}

pub fn short_path(p: &str) -> String {
    let home = std::env::var("HOME").unwrap_or_default();
    if !home.is_empty() {
        if p == home {
            return "~".into();
        }
        if let Some(rest) = p.strip_prefix(&format!("{home}/")) {
            return format!("~/{rest}");
        }
    }
    p.to_string()
}

/// Strip a leading "[x] " or "[xx] " hint we put there ourselves, so a popup
/// that died with hints still up (or the `--clear` helper) sees the original
/// label.
pub fn strip_hint(s: &str) -> &str {
    let Some(rest) = s.strip_prefix('[') else {
        return s;
    };
    let Some(end) = rest.find("] ") else {
        return s;
    };
    let tag = &rest[..end];
    let ok = (1..=2).contains(&tag.len())
        && tag
            .chars()
            .all(|c| ALPHABET.contains(c) || c.is_ascii_digit());
    if ok {
        &rest[end + 2..]
    } else {
        s
    }
}

pub struct Model {
    pub focused_pane: Option<String>,
    pub current_tab: Option<String>,
    pub current_ws: Option<String>,
    pub panes: HashMap<String, Pane>,
    pub agents: HashMap<String, Agent>,
    pub workspaces: HashMap<String, Workspace>,
    pub layout: Option<Layout>,
    pub tab_panes: Vec<LayoutPane>,
    pub ws_rows: Vec<(Workspace, Vec<Tab>)>,
    pub current_ws_tabs: Vec<Tab>,
    /// Agent panes in sidebar order (sidebar mode only).
    pub agent_order: Vec<String>,
    /// Columns left of the pane area, i.e. the sidebar's width. -1 if unknown.
    pub sidebar_cols: i64,
    /// Outer terminal size, derived from the pane area. (0, 0) if unknown.
    pub screen_cols: usize,
    pub screen_rows: usize,
    pub dests: Vec<Dest>,
    pub labels: Vec<String>, // parallel to dests
    label_index: HashMap<Dest, usize>,
}

impl Model {
    pub fn new(snap: Snapshot, ctx: Context, mode: Mode, sort: AgentSort) -> Self {
        let current_tab = ctx.tab_id.or(snap.focused_tab_id);
        let current_ws = ctx.workspace_id.or(snap.focused_workspace_id);

        let panes: HashMap<_, _> = snap
            .panes
            .into_iter()
            .map(|p| (p.pane_id.clone(), p))
            .collect();
        let agents: HashMap<_, _> = snap
            .agents
            .into_iter()
            .map(|a| (a.pane_id.clone(), a))
            .collect();
        let tabs: HashMap<_, _> = snap
            .tabs
            .into_iter()
            .map(|mut t| {
                t.label = strip_hint(&t.label).to_string();
                (t.tab_id.clone(), t)
            })
            .collect();
        let workspaces: HashMap<_, _> = snap
            .workspaces
            .into_iter()
            .map(|w| (w.workspace_id.clone(), w))
            .collect();

        let layout = snap
            .layouts
            .iter()
            .find(|l| Some(&l.tab_id) == current_tab.as_ref())
            .cloned();
        let probe = layout.as_ref().or(snap.layouts.first());
        // Herdr 0.8 reports the pane area in outer-terminal coordinates, so
        // `area.x` is the sidebar's width (33 expanded, 4 collapsed rail).
        // Herdr 0.9 renders the sidebar in the client and reports the pane
        // area relative to itself: `x` is always 0 and says nothing about
        // the sidebar, so treat 0 as unknown and assume it is expanded.
        let (sidebar_cols, screen_cols, screen_rows) = match probe {
            Some(l) => (
                if l.area.x > 0 { l.area.x } else { -1 },
                (l.area.x + l.area.width).max(0) as usize,
                (l.area.y + l.area.height).max(0) as usize,
            ),
            None => (-1, 0, 0),
        };
        // EASYJUMP_SIDEBAR=collapsed|expanded overrides the guess (debugging).
        let sidebar_cols = match std::env::var("EASYJUMP_SIDEBAR").as_deref() {
            Ok("collapsed") => 0,
            Ok("expanded") => SIDEBAR_MIN_COLS.max(sidebar_cols),
            _ => sidebar_cols,
        };
        let mut tab_panes = layout.as_ref().map(|l| l.panes.clone()).unwrap_or_default();
        tab_panes.sort_by_key(|p| (p.rect.y, p.rect.x));

        let mut ws_sorted: Vec<Workspace> = workspaces.values().cloned().collect();
        ws_sorted.sort_by_key(|w| w.number);
        let mut ws_rows = Vec::new();
        for ws in ws_sorted {
            let mut wtabs: Vec<Tab> = tabs
                .values()
                .filter(|t| t.workspace_id == ws.workspace_id)
                .cloned()
                .collect();
            wtabs.sort_by_key(|t| t.number);
            ws_rows.push((ws, wtabs));
        }
        let current_ws_tabs = ws_rows
            .iter()
            .find(|(w, _)| Some(&w.workspace_id) == current_ws.as_ref())
            .map(|(_, t)| t.clone())
            .unwrap_or_default();

        let mut dests: Vec<Dest> = Vec::new();
        let mut seen: HashSet<Dest> = HashSet::new();
        let mut agent_order: Vec<String> = Vec::new();
        let mut add = |kind: Kind, id: &str| {
            let d = (kind, id.to_string());
            if seen.insert(d.clone()) {
                dests.push(d);
            }
        };
        let tab_number = |pane_id: &str| -> i64 {
            panes
                .get(pane_id)
                .and_then(|p| tabs.get(&p.tab_id))
                .map(|t| t.number)
                .unwrap_or(0)
        };

        match mode {
            Mode::Sidebar => {
                let mut listed: Vec<&Agent> = Vec::new();
                for (ws, _) in &ws_rows {
                    add(Kind::Workspace, &ws.workspace_id);
                    let mut here: Vec<&Agent> = agents
                        .values()
                        .filter(|a| a.workspace_id == ws.workspace_id)
                        .collect();
                    here.sort_by_key(|a| (tab_number(&a.pane_id), a.pane_id.clone()));
                    listed.extend(here);
                }
                agent_sort::order(&mut listed, sort);
                for a in listed {
                    add(Kind::Pane, &a.pane_id);
                    agent_order.push(a.pane_id.clone());
                }
                for p in &tab_panes {
                    add(Kind::Pane, &p.pane_id);
                }
                if current_ws_tabs.len() > 1 {
                    for t in &current_ws_tabs {
                        add(Kind::Tab, &t.tab_id);
                    }
                }
            }
            Mode::List => {
                for p in &tab_panes {
                    add(Kind::Pane, &p.pane_id);
                }
                let mut all: Vec<&Agent> = agents.values().collect();
                all.sort_by_key(|a| {
                    (
                        Reverse(agent_sort::rank(a.agent_status.as_deref())),
                        Reverse(a.state_change_seq),
                        a.pane_id.clone(),
                    )
                });
                for a in all {
                    add(Kind::Pane, &a.pane_id);
                }
                for (ws, wtabs) in &ws_rows {
                    if wtabs.len() == 1 {
                        add(Kind::Tab, &wtabs[0].tab_id);
                    } else {
                        add(Kind::Workspace, &ws.workspace_id);
                        for t in wtabs {
                            add(Kind::Tab, &t.tab_id);
                        }
                    }
                }
            }
        }

        let roles: Vec<Role> = dests
            .iter()
            .map(|(kind, id)| match kind {
                Kind::Workspace => Role::Space(
                    workspaces
                        .get(id)
                        .map(|w| w.label.clone())
                        .unwrap_or_default(),
                ),
                Kind::Pane if agents.contains_key(id) => Role::Agent,
                Kind::Pane => Role::Pane,
                Kind::Tab => Role::Tab,
            })
            .collect();
        let labels = assign_labels(&roles);
        let label_index = dests
            .iter()
            .cloned()
            .enumerate()
            .map(|(i, d)| (d, i))
            .collect();

        Model {
            focused_pane: snap.focused_pane_id,
            current_tab,
            current_ws,
            panes,
            agents,
            workspaces,
            layout,
            tab_panes,
            ws_rows,
            current_ws_tabs,
            agent_order,
            sidebar_cols,
            screen_cols,
            screen_rows,
            dests,
            labels,
            label_index,
        }
    }

    /// True when the sidebar is too narrow to show the hint tokens, so the
    /// HUD has to list spaces and agents itself. Unknown counts as expanded.
    pub fn sidebar_collapsed(&self) -> bool {
        self.sidebar_cols >= 0 && self.sidebar_cols < SIDEBAR_MIN_COLS
    }

    pub fn label(&self, kind: Kind, id: &str) -> &str {
        self.label_index
            .get(&(kind, id.to_string()))
            .map(|&i| self.labels[i].as_str())
            .unwrap_or("")
    }

    pub fn dest_for(&self, label: &str) -> Option<&Dest> {
        self.labels
            .iter()
            .position(|l| l == label)
            .map(|i| &self.dests[i])
    }

    pub fn labels_with_prefix(&self, prefix: &str) -> Vec<&str> {
        self.labels
            .iter()
            .filter(|l| l.starts_with(prefix))
            .map(String::as_str)
            .collect()
    }

    pub fn jump(&self, client: &mut Client, dest: &Dest) -> Result<(), String> {
        let (method, key) = match dest.0 {
            Kind::Pane => ("pane.focus", "pane_id"),
            Kind::Tab => ("tab.focus", "tab_id"),
            Kind::Workspace => ("workspace.focus", "workspace_id"),
        };
        client.call(method, json!({ key: dest.1 })).map(|_| ())
    }

    /// (primary, secondary) text for a pane.
    pub fn pane_summary(&self, pane_id: &str) -> (String, String) {
        if let Some(a) = self.agents.get(pane_id) {
            let primary = a
                .name
                .clone()
                .or_else(|| a.display_agent.clone())
                .or_else(|| a.agent.clone())
                .unwrap_or_else(|| "agent".into());
            let secondary = a
                .terminal_title_stripped
                .clone()
                .unwrap_or_else(|| short_path(a.cwd.as_deref().unwrap_or("")));
            return (primary, secondary);
        }
        let p = self.panes.get(pane_id);
        let cwd = p.and_then(|p| p.cwd.clone()).unwrap_or_default();
        let primary = p
            .and_then(|p| p.label.clone())
            .filter(|s| !s.is_empty())
            .or_else(|| {
                cwd.rsplit('/')
                    .next()
                    .filter(|s| !s.is_empty())
                    .map(String::from)
            })
            .unwrap_or_else(|| "shell".into());
        let secondary = p
            .and_then(|p| p.terminal_title_stripped.clone())
            .unwrap_or_else(|| short_path(&cwd));
        (primary, secondary)
    }

    pub fn agent_status(&self, pane_id: &str) -> Option<&str> {
        self.agents
            .get(pane_id)
            .and_then(|a| a.agent_status.as_deref())
    }

    /// (method, id_key, dest) for every destination the sidebar shows.
    fn sidebar_targets(&self) -> Vec<(&'static str, &'static str, &Dest)> {
        self.dests
            .iter()
            .filter_map(|d| match d.0 {
                Kind::Workspace => Some(("workspace.report_metadata", "workspace_id", d)),
                Kind::Pane if self.agents.contains_key(&d.1) => {
                    Some(("pane.report_metadata", "pane_id", d))
                }
                _ => None,
            })
            .collect()
    }

    /// Text the pane border shows behind the hint: agent name, or the
    /// terminal title / cwd of a plain shell.
    fn border_text(&self, pane_id: &str) -> String {
        let (primary, secondary) = self.pane_summary(pane_id);
        if self.agents.contains_key(pane_id) {
            return primary;
        }
        let title = self
            .panes
            .get(pane_id)
            .and_then(|p| p.terminal_title_stripped.clone())
            .unwrap_or_default();
        if title.is_empty() {
            secondary
        } else {
            title
        }
    }

    /// Tabs of the current workspace that show a hint: the tab bar has no
    /// metadata slot, so the hint is a prefix on the tab's label.
    fn renamed_tabs(&self) -> Vec<&Tab> {
        self.current_ws_tabs
            .iter()
            .filter(|t| {
                self.label_index
                    .contains_key(&(Kind::Tab, t.tab_id.clone()))
            })
            .collect()
    }

    /// Push (or refresh) every visible hint: the $hint token on sidebar rows,
    /// the title override on the borders of the current tab's panes, and a
    /// label prefix on tabs. Hints that no longer match `typed`
    /// are withdrawn.
    pub fn publish_hints(&self, client: &mut Client, typed: &str) {
        // One report_metadata call per pane, even when it is both an agent
        // row in the sidebar and a pane of the current tab.
        let mut pane_params: HashMap<String, Value> = HashMap::new();
        for (method, key, dest) in self.sidebar_targets() {
            let label = self.label(dest.0.clone(), &dest.1);
            let mut params = json!({ key: dest.1, "source": SOURCE, "ttl_ms": TOKEN_TTL_MS });
            if label.starts_with(typed) {
                params["tokens"] = json!({ TOKEN: format!("[{label}]") });
            } else {
                params["tokens"] = json!({ TOKEN: Value::Null });
            }
            if method == "workspace.report_metadata" {
                let _ = client.call(method, params);
            } else {
                pane_params.insert(dest.1.clone(), params);
            }
        }
        for p in &self.tab_panes {
            let label = self.label(Kind::Pane, &p.pane_id);
            let entry = pane_params.entry(p.pane_id.clone()).or_insert_with(
                || json!({ "pane_id": p.pane_id, "source": SOURCE, "ttl_ms": TOKEN_TTL_MS }),
            );
            if label.starts_with(typed) {
                entry["title"] = json!(format!("[{label}] {}", self.border_text(&p.pane_id)));
            } else {
                entry["clear_title"] = json!(true);
            }
        }
        for params in pane_params.into_values() {
            let _ = client.call("pane.report_metadata", params);
        }
        for t in self.renamed_tabs() {
            let label = self.label(Kind::Tab, &t.tab_id);
            let new = if label.starts_with(typed) {
                format!("[{label}] {}", t.label)
            } else {
                t.label.clone()
            };
            let _ = client.call("tab.rename", json!({ "tab_id": t.tab_id, "label": new }));
        }
    }

    /// Withdraw every hint this model published.
    pub fn clear_hints(&self, client: &mut Client) {
        let mut pane_ids: HashSet<&str> = HashSet::new();
        for (method, key, dest) in self.sidebar_targets() {
            if method == "workspace.report_metadata" {
                let _ = client.call(
                    method,
                    json!({ key: dest.1, "source": SOURCE, "tokens": { TOKEN: Value::Null } }),
                );
            } else {
                pane_ids.insert(&dest.1);
            }
        }
        for p in &self.tab_panes {
            pane_ids.insert(&p.pane_id);
        }
        for id in pane_ids {
            let _ = client.call(
                "pane.report_metadata",
                json!({ "pane_id": id, "source": SOURCE, "tokens": { TOKEN: Value::Null }, "clear_title": true }),
            );
        }
        for t in self.renamed_tabs() {
            let _ = client.call(
                "tab.rename",
                json!({ "tab_id": t.tab_id, "label": t.label }),
            );
        }
    }

    /// Clear every hint on every workspace, pane and tab, whether or not this
    /// process published it. Used by the detached `--clear` helper, which
    /// only has a fresh snapshot to go on.
    pub fn clear_all(client: &mut Client, snap: &Snapshot) {
        for w in &snap.workspaces {
            let _ = client.call(
                "workspace.report_metadata",
                json!({ "workspace_id": w.workspace_id, "source": SOURCE, "tokens": { TOKEN: Value::Null } }),
            );
        }
        for p in &snap.panes {
            let _ = client.call(
                "pane.report_metadata",
                json!({ "pane_id": p.pane_id, "source": SOURCE, "tokens": { TOKEN: Value::Null }, "clear_title": true }),
            );
        }
        for t in &snap.tabs {
            let orig = strip_hint(&t.label);
            if orig != t.label {
                let _ = client.call("tab.rename", json!({ "tab_id": t.tab_id, "label": orig }));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn space(name: &str) -> Role {
        Role::Space(name.into())
    }

    #[test]
    fn agents_take_digits_then_letters_the_rest_left_over() {
        let mut roles = vec![space("work")];
        roles.extend(vec![Role::Agent; 12]);
        let labels = assign_labels(&roles);
        assert_eq!(labels[0], "w");
        assert_eq!(labels[1..10], ["1", "2", "3", "4", "5", "6", "7", "8", "9"]);
        assert_eq!(labels[10], "0");
        // 11th and 12th agents: first free letters, skipping the space's "w".
        assert_eq!(labels[11], "a");
        assert_eq!(labels[12], "s");
    }

    #[test]
    fn space_takes_next_free_letter_of_its_name_on_collision() {
        let labels = assign_labels(&[space("api"), space("app"), space("alpha")]);
        // "app": a taken, p free. "alpha": a taken, l free.
        assert_eq!(labels, ["a", "p", "l"]);
    }

    #[test]
    fn space_falls_back_to_first_free_alphabet_letter() {
        // "aa" has no second letter to try, "1-" has no letters at all.
        let labels = assign_labels(&[space("a"), space("aa"), space("1-")]);
        assert_eq!(labels, ["a", "s", "d"]);
    }

    #[test]
    fn spaces_never_take_the_reserved_quit_key() {
        let labels = assign_labels(&[space("queue"), space("Quiet")]);
        // "queue": q is reserved, u is free. "Quiet": q reserved, u taken, i free.
        assert_eq!(labels, ["u", "i"]);
    }

    #[test]
    fn tabs_then_panes_get_letters_the_spaces_did_not_use() {
        let labels = assign_labels(&[
            space("alpha"),
            space("sigma"),
            Role::Pane,
            Role::Tab,
            Role::Tab,
        ]);
        assert_eq!(labels, ["a", "s", "g", "d", "f"]);
    }

    #[test]
    fn adding_a_pane_does_not_change_any_tab_label() {
        let tab_labels = |roles: &[Role]| -> Vec<String> {
            assign_labels(roles)
                .into_iter()
                .zip(roles)
                .filter(|(_, r)| **r == Role::Tab)
                .map(|(l, _)| l)
                .collect()
        };
        let before = [space("alpha"), Role::Tab, Role::Pane, Role::Tab, Role::Pane];
        let after = [
            space("alpha"),
            Role::Tab,
            Role::Pane,
            Role::Pane,
            Role::Tab,
            Role::Pane,
        ];
        assert_eq!(tab_labels(&before), tab_labels(&after));
    }

    #[test]
    fn spaces_are_labelled_before_tabs_and_panes_regardless_of_order() {
        let labels = assign_labels(&[Role::Pane, space("zed")]);
        assert_eq!(labels, ["a", "z"]);
    }

    #[test]
    fn too_many_letter_targets_switch_all_of_them_to_two_letters() {
        let mut roles = vec![Role::Agent];
        roles.extend(vec![Role::Pane; ALPHABET.len() + 1]);
        let labels = assign_labels(&roles);
        assert_eq!(labels[0], "1");
        assert_eq!(labels[1], "aa");
        assert_eq!(labels[2], "as");
        let mut seen = HashSet::new();
        assert!(labels.iter().all(|l| seen.insert(l.clone())));
        // No label is a prefix of another, so typing never jumps early.
        for a in &labels {
            for b in &labels {
                assert!(a == b || !b.starts_with(a.as_str()));
            }
        }
    }

    #[test]
    fn hint_prefix_is_stripped_for_digits_and_letters() {
        assert_eq!(strip_hint("[3] build"), "build");
        assert_eq!(strip_hint("[j] build"), "build");
        assert_eq!(strip_hint("[as] build"), "build");
        assert_eq!(strip_hint("[abc] build"), "[abc] build");
        assert_eq!(strip_hint("build"), "build");
    }
}
