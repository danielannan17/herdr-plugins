//! The order of the sidebar's agent list, as Herdr draws it.
//!
//! Herdr 0.9.5 (`src/client/shell/agent_sidebar.rs`, `ordered_agent_pane_ids`):
//! - `spaces` (default, shown as "grouped"): workspace order, then tab order.
//! - `priority`: one flat list across all workspaces, by status rank
//!   (blocked > done > working > idle > unknown), then by most recent state
//!   change first. Equal keys keep the `spaces` order.
//!
//! The sort in effect is the per-client preference toggled by clicking the
//! sidebar's sort label, else `ui.agent_panel_sort` from `config.toml`.

use crate::model::Agent;
use std::cmp::Reverse;
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum AgentSort {
    #[default]
    Spaces,
    Priority,
}

impl AgentSort {
    fn parse(s: &str) -> Option<Self> {
        match s {
            "spaces" | "workspaces" => Some(AgentSort::Spaces),
            "priority" => Some(AgentSort::Priority),
            _ => None,
        }
    }

    /// The sort the user's Herdr client is showing right now.
    pub fn current() -> Self {
        client_preference()
            .or_else(config_value)
            .unwrap_or_default()
    }
}

/// Herdr's `status_priority`: higher sorts first. Herdr calls an idle agent
/// that has not been looked at yet `done`.
pub fn rank(status: Option<&str>) -> u8 {
    match status {
        Some("blocked") => 4,
        Some("done") => 3,
        Some("working") => 2,
        Some("idle") => 1,
        _ => 0,
    }
}

/// Order `agents`, given in `spaces` order, the way the sidebar lists them.
pub fn order(agents: &mut [&Agent], sort: AgentSort) {
    if sort == AgentSort::Priority {
        agents.sort_by_key(|a| {
            (
                Reverse(rank(a.agent_status.as_deref())),
                Reverse(a.state_change_seq),
            )
        });
    }
}

fn config_path() -> PathBuf {
    if let Some(p) = env_path("HERDR_CONFIG_PATH") {
        return p;
    }
    let dir = match env_path("XDG_CONFIG_HOME") {
        Some(xdg) => xdg.join("herdr"),
        None => home().join(".config/herdr"),
    };
    dir.join("config.toml")
}

fn state_dir() -> PathBuf {
    match env_path("XDG_STATE_HOME") {
        Some(xdg) => xdg.join("herdr"),
        None => home().join(".local/state/herdr"),
    }
}

fn env_path(key: &str) -> Option<PathBuf> {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
}

fn home() -> PathBuf {
    env_path("HOME").unwrap_or_else(std::env::temp_dir)
}

fn config_value() -> Option<AgentSort> {
    let text = std::fs::read_to_string(config_path()).ok()?;
    sort_from_config(&text)
}

fn sort_from_config(text: &str) -> Option<AgentSort> {
    let doc: toml::Table = text.parse().ok()?;
    AgentSort::parse(doc.get("ui")?.get("agent_panel_sort")?.as_str()?)
}

/// Herdr stores a manual sort toggle per client, in a file named after a hash
/// of the client socket, which sits next to the API socket.
fn client_preference() -> Option<AgentSort> {
    let api_socket = PathBuf::from(crate::rpc::socket_path());
    let client_socket = api_socket.parent()?.join("herdr-client.sock");
    let file = state_dir()
        .join("client-shell")
        .join(format!("local-{:016x}.json", socket_hash(&client_socket)));
    sort_from_preferences(&std::fs::read_to_string(file).ok()?)
}

fn sort_from_preferences(text: &str) -> Option<AgentSort> {
    let prefs: serde_json::Value = serde_json::from_str(text).ok()?;
    AgentSort::parse(prefs.get("agent_panel_sort")?.as_str()?)
}

/// FNV-1a over the socket path, as Herdr's `path_for_local_endpoint`.
fn socket_hash(socket: &Path) -> u64 {
    socket
        .to_string_lossy()
        .bytes()
        .fold(0xcbf29ce484222325u64, |h, b| {
            (h ^ u64::from(b)).wrapping_mul(0x100000001b3)
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn agent(id: &str, status: &str, seq: u64) -> Agent {
        Agent {
            pane_id: id.into(),
            agent_status: Some(status.into()),
            state_change_seq: seq,
            ..Default::default()
        }
    }

    fn ordered(agents: &[Agent], sort: AgentSort) -> Vec<&str> {
        let mut refs: Vec<&Agent> = agents.iter().collect();
        order(&mut refs, sort);
        refs.iter().map(|a| a.pane_id.as_str()).collect()
    }

    #[test]
    fn spaces_keeps_the_given_order() {
        let agents = [agent("a", "idle", 1), agent("b", "blocked", 9)];
        assert_eq!(ordered(&agents, AgentSort::Spaces), ["a", "b"]);
    }

    #[test]
    fn priority_ranks_blocked_done_working_idle_unknown() {
        let agents = [
            agent("unknown", "unknown", 5),
            agent("idle", "idle", 5),
            agent("working", "working", 5),
            agent("done", "done", 5),
            agent("blocked", "blocked", 5),
        ];
        assert_eq!(
            ordered(&agents, AgentSort::Priority),
            ["blocked", "done", "working", "idle", "unknown"]
        );
    }

    #[test]
    fn priority_puts_the_most_recent_state_change_first_within_a_rank() {
        let agents = [
            agent("old", "working", 3),
            agent("new", "working", 20),
            agent("mid", "working", 7),
        ];
        assert_eq!(ordered(&agents, AgentSort::Priority), ["new", "mid", "old"]);
    }

    #[test]
    fn priority_ties_keep_the_spaces_order() {
        let agents = [agent("first", "idle", 4), agent("second", "idle", 4)];
        assert_eq!(ordered(&agents, AgentSort::Priority), ["first", "second"]);
    }

    #[test]
    fn missing_status_ranks_as_unknown() {
        assert_eq!(rank(None), rank(Some("unknown")));
        assert_eq!(rank(Some("anything else")), 0);
    }

    #[test]
    fn config_value_is_read_from_the_ui_table() {
        let toml = "[ui]\nstatus_indicators = \"symbols\"\nagent_panel_sort = \"priority\"\n\n[ui.toast]\ndelivery = \"herdr\"\n";
        assert_eq!(sort_from_config(toml), Some(AgentSort::Priority));
        assert_eq!(
            sort_from_config("[ui]\nagent_panel_sort = \"workspaces\"\n"),
            Some(AgentSort::Spaces)
        );
        assert_eq!(
            sort_from_config("ui.agent_panel_sort = \"priority\"\n"),
            Some(AgentSort::Priority)
        );
        assert_eq!(sort_from_config("[ui]\nwhich_key = true\n"), None);
        assert_eq!(sort_from_config("not toml ["), None);
    }

    #[test]
    fn client_preference_is_read_from_its_json() {
        assert_eq!(
            sort_from_preferences(
                "{\"sidebar_section_split\": 0.5, \"agent_panel_sort\": \"priority\"}"
            ),
            Some(AgentSort::Priority)
        );
        assert_eq!(sort_from_preferences("{}"), None);
    }

    #[test]
    fn socket_hash_matches_herdr() {
        // Herdr names this client's preference file local-e7a2283250218e59.json.
        assert_eq!(
            socket_hash(Path::new("/Users/daniel/.config/herdr/herdr-client.sock")),
            0xe7a2283250218e59
        );
    }
}
