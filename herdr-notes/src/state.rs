//! Persistent note state: one scrollable markdown note PER WORKSPACE, one
//! GLOBAL note shared by every workspace (`_global.json`), plus the last-active
//! mode of each and the selected tab (`_view.json`), stored as small JSON files
//! beside herdr's own config
//! (`%APPDATA%\herdr\notes\<workspace-id>.json` on Windows,
//! `$XDG_CONFIG_HOME/herdr/…` elsewhere) so the note survives computer
//! restarts. The key is the stable `HERDR_WORKSPACE_ID` herdr injects into
//! every managed pane; outside herdr (or on an id unsafe for a filename) the
//! pane falls back to the legacy single-note `herdr/notes.json`, and the
//! first workspace to load notes MOVES that legacy file into its own slot.
//!
//! Loading is forgiving — a missing, hand-edited, or truncated file falls back
//! to an empty note and never panics. Saving is atomic (temp file + rename)
//! and best-effort: the pane keeps working for the session if persist fails.

use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// Pane label the launcher assigns and the heartbeat re-asserts as the title.
pub const PANE_LABEL: &str = "Notes";

/// Source id for `pane.report_metadata`; its token marks a pane as the Notes
/// pane and doubles as the liveness heartbeat.
pub const METADATA_SOURCE: &str = "herdr-notes";

/// Unix seconds now — the heartbeat clock for the pane identity token.
pub fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Mode {
    #[default]
    Preview,
    Edit,
}

impl Mode {
    fn name(self) -> &'static str {
        match self {
            Mode::Preview => "preview",
            Mode::Edit => "edit",
        }
    }
}

#[derive(Clone, PartialEq, Eq, Debug, Default)]
pub struct Note {
    /// Raw markdown of the note.
    pub text: String,
    pub mode: Mode,
}

/// Which note the pane shows: this workspace's, or the one every workspace
/// shares.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Tab {
    #[default]
    Workspace,
    Global,
}

impl Tab {
    fn name(self) -> &'static str {
        match self {
            Tab::Workspace => "workspace",
            Tab::Global => "global",
        }
    }
}

/// File names shared by every workspace. The leading underscore keeps them
/// clear of workspace keys, which are alphanumeric (see `is_filename_safe`).
const GLOBAL_FILE: &str = "_global.json";
const VIEW_FILE: &str = "_view.json";

/// Where notes live. herdr's plugin docs say durable state belongs in
/// `HERDR_PLUGIN_STATE_DIR`, which herdr injects into plugin-run commands
/// (the unix `[[panes]]` entry gets it natively; the Windows launcher passes
/// it through `pane split --env`). A TUI started by hand has neither, so the
/// pre-existing config-dir layout stays as the fallback — and as the
/// migration source when the state dir is empty.
enum StoreBase {
    /// `HERDR_PLUGIN_STATE_DIR`: files live directly in the dir
    /// (`<dir>/<key>.json`, no-workspace fallback `<dir>/note.json`).
    PluginState(PathBuf),
    /// Config-dir layout (`<config>/herdr/notes/<key>.json`, legacy
    /// `<config>/herdr/notes.json`).
    Config(PathBuf),
}

fn store_base() -> Option<StoreBase> {
    if let Some(dir) = std::env::var_os("HERDR_PLUGIN_STATE_DIR").filter(|d| !d.is_empty()) {
        return Some(StoreBase::PluginState(PathBuf::from(dir)));
    }
    config_base().map(StoreBase::Config)
}

/// Platform config base (`%APPDATA%` / `$XDG_CONFIG_HOME` / `~/.config`),
/// the convention herdr plugins used before `HERDR_PLUGIN_STATE_DIR` existed.
/// All path logic below takes this as a parameter so tests can inject a temp
/// dir.
fn config_base() -> Option<PathBuf> {
    #[cfg(windows)]
    let base = std::env::var_os("APPDATA").map(PathBuf::from);
    #[cfg(not(windows))]
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")));
    base
}

/// The workspace id herdr injects into every managed pane; the per-workspace
/// note key. Empty = unset (running outside herdr).
fn workspace_env() -> Option<String> {
    std::env::var("HERDR_WORKSPACE_ID").ok().filter(|id| !id.is_empty())
}

/// True when the workspace id is safe to embed in a filename. Stricter than
/// launch.rs's flag-safe check (which also admits `:` `.` `_` `-`): real ids
/// are plain alphanumeric ("w6"), and anything else — separators, dots,
/// anything path-traversal-shaped — falls back to the legacy path instead.
fn is_filename_safe(id: &str) -> bool {
    !id.is_empty() && id.chars().all(|c| c.is_ascii_alphanumeric())
}

/// Pre-per-workspace single-note file; also the fallback when no (safe)
/// workspace id is available.
fn legacy_path_in(base: &Path) -> PathBuf {
    base.join("herdr").join("notes.json")
}

/// The note-FILE identity of a workspace id: `Some(key)` when the id gets its
/// own per-workspace file, `None` when it falls back to the shared legacy
/// `notes.json`. Panes whose keys are EQUAL load and save the SAME file.
/// This is the identity the launcher's duplicate-instance guard (launch.rs)
/// compares — never raw workspace ids — so the guard can't drift from the
/// on-disk layout: unsafe/missing ids all coarsen to one legacy file, and on
/// Windows ASCII case is folded because NTFS filenames are case-insensitive
/// ("W6.json" and "w6.json" are one file).
pub fn note_key(workspace_id: Option<&str>) -> Option<String> {
    let id = workspace_id.filter(|id| is_filename_safe(id))?;
    #[cfg(windows)]
    let key = id.to_ascii_lowercase();
    #[cfg(not(windows))]
    let key = id.to_string();
    Some(key)
}

/// Pure path selection: `<base>/herdr/notes/<note-key>.json` for a
/// filename-safe id, the legacy `<base>/herdr/notes.json` otherwise.
/// Built from [`note_key`] so path identity and guard identity always agree.
fn state_path_in(base: &Path, workspace_id: Option<&str>) -> PathBuf {
    match note_key(workspace_id) {
        Some(key) => base.join("herdr").join("notes").join(format!("{key}.json")),
        None => legacy_path_in(base),
    }
}

/// Path selection for the plugin-state layout: `<dir>/<note-key>.json`, with
/// the shared `<dir>/note.json` for missing/unsafe workspace ids.
fn state_dir_path(dir: &Path, workspace_id: Option<&str>) -> PathBuf {
    match note_key(workspace_id) {
        Some(key) => dir.join(format!("{key}.json")),
        None => dir.join("note.json"),
    }
}

/// Path of a file shared by every workspace, beside the workspace notes in
/// either layout.
fn shared_path(base: &StoreBase, name: &str) -> PathBuf {
    match base {
        StoreBase::PluginState(dir) => dir.join(name),
        StoreBase::Config(base) => base.join("herdr").join("notes").join(name),
    }
}

/// Where THIS process keeps its notes: the env-derived base plus the
/// workspace id.
pub struct Store {
    base: StoreBase,
    /// Migration source for the plugin-state layout.
    config: Option<PathBuf>,
    workspace: Option<String>,
}

impl Store {
    /// `None` when there is nowhere to persist; the pane then runs unsaved.
    pub fn from_env() -> Option<Self> {
        Some(Self { base: store_base()?, config: config_base(), workspace: workspace_env() })
    }

    #[cfg(test)]
    pub fn in_plugin_dir(dir: PathBuf, workspace: &str) -> Self {
        Self {
            base: StoreBase::PluginState(dir),
            config: None,
            workspace: Some(workspace.to_string()),
        }
    }

    fn path(&self, tab: Tab) -> PathBuf {
        match (tab, &self.base) {
            (Tab::Global, base) => shared_path(base, GLOBAL_FILE),
            (Tab::Workspace, StoreBase::PluginState(dir)) => {
                state_dir_path(dir, self.workspace.as_deref())
            }
            (Tab::Workspace, StoreBase::Config(base)) => {
                state_path_in(base, self.workspace.as_deref())
            }
        }
    }

    /// The global note never migrates: there is no older layout for it.
    pub fn load(&self, tab: Tab) -> Note {
        match (tab, &self.base) {
            (Tab::Global, _) => read_note(&self.path(tab)),
            (Tab::Workspace, StoreBase::PluginState(dir)) => {
                load_state_dir(dir, self.config.as_deref(), self.workspace.as_deref())
            }
            (Tab::Workspace, StoreBase::Config(base)) => load_in(base, self.workspace.as_deref()),
        }
    }

    pub fn save(&self, tab: Tab, note: &Note) {
        write_atomic(&self.path(tab), &to_json(note));
    }

    /// Modification time of a note's file; `None` when it does not exist.
    pub fn mtime(&self, tab: Tab) -> Option<SystemTime> {
        std::fs::metadata(self.path(tab)).and_then(|m| m.modified()).ok()
    }

    pub fn load_tab(&self) -> Tab {
        std::fs::read_to_string(shared_path(&self.base, VIEW_FILE))
            .map(|json| parse_tab(&json))
            .unwrap_or_default()
    }

    pub fn save_tab(&self, tab: Tab) {
        write_atomic(&shared_path(&self.base, VIEW_FILE), &tab_to_json(tab));
    }
}

/// Load from the plugin state dir, migrating from the config-dir layout the
/// first time: if this workspace's file is missing there, MOVE the config-dir
/// per-workspace file (or, failing that, the legacy single note) into place.
/// A failed rename falls back to reading the source without moving it.
fn load_state_dir(dir: &Path, config: Option<&Path>, workspace_id: Option<&str>) -> Note {
    let path = state_dir_path(dir, workspace_id);
    if !path.exists()
        && let Some(base) = config
    {
        let sources = [state_path_in(base, workspace_id), legacy_path_in(base)];
        if let Some(src) = sources.iter().find(|p| p.exists()) {
            let moved = std::fs::create_dir_all(dir).is_ok() && std::fs::rename(src, &path).is_ok();
            if !moved {
                return read_note(src);
            }
        }
    }
    read_note(&path)
}

/// Load with one-time migration: when the per-workspace file does not exist
/// yet but the legacy single-note file does, MOVE the legacy file into this
/// workspace's slot — the first workspace to open notes inherits the old note.
/// If the rename fails the legacy file is read in place (not moved); when both
/// files exist the per-workspace one wins and the legacy file is untouched.
fn load_in(base: &Path, workspace_id: Option<&str>) -> Note {
    let path = state_path_in(base, workspace_id);
    let legacy = legacy_path_in(base);
    if path != legacy && !path.exists() && legacy.exists() {
        let moved = path.parent().is_some_and(|dir| {
            std::fs::create_dir_all(dir).is_ok() && std::fs::rename(&legacy, &path).is_ok()
        });
        if !moved {
            return read_note(&legacy);
        }
    }
    read_note(&path)
}

fn read_note(path: &Path) -> Note {
    std::fs::read_to_string(path).map(|json| parse(&json)).unwrap_or_default()
}

/// Forgiving parse: any missing/garbled field falls back to the default, so a
/// hand-edited or truncated file can never wedge the pane.
pub fn parse(json: &str) -> Note {
    let value: serde_json::Value = match serde_json::from_str(json.trim_start_matches('\u{feff}')) {
        Ok(v) => v,
        Err(_) => return Note::default(),
    };
    let text = value
        .get("text")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();
    let mode = match value.get("mode").and_then(|v| v.as_str()) {
        Some("edit") => Mode::Edit,
        _ => Mode::Preview,
    };
    Note { text, mode }
}

/// The JSON that goes on disk: `{ "text": …, "mode": "preview"|"edit" }`.
pub fn to_json(note: &Note) -> String {
    serde_json::json!({
        "text": note.text,
        "mode": note.mode.name(),
    })
    .to_string()
}

/// Forgiving parse of `_view.json`: anything but `{"tab":"global"}` is the
/// workspace tab.
pub fn parse_tab(json: &str) -> Tab {
    let value: serde_json::Value =
        serde_json::from_str(json.trim_start_matches('\u{feff}')).unwrap_or_default();
    match value.get("tab").and_then(|v| v.as_str()) {
        Some("global") => Tab::Global,
        _ => Tab::Workspace,
    }
}

/// The JSON that goes on disk: `{ "tab": "workspace"|"global" }`.
pub fn tab_to_json(tab: Tab) -> String {
    serde_json::json!({ "tab": tab.name() }).to_string()
}

/// Atomic best-effort persist: write a temp file, fsync it, then rename over
/// the real one (std's rename replaces existing files on Windows too). The
/// fsync BEFORE the rename matters: without it a crash or power loss can make
/// the rename durable ahead of the data, leaving an empty/truncated file the
/// forgiving loader would silently turn into an empty note.
fn write_atomic(path: &Path, contents: &str) {
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let tmp = path.with_extension("json.tmp");
    let written = std::fs::File::create(&tmp).and_then(|mut f| {
        use std::io::Write;
        f.write_all(contents.as_bytes())?;
        f.sync_all()
    });
    if written.is_ok() {
        let _ = std::fs::rename(&tmp, path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_preserves_text_and_mode() {
        let note = Note { text: "# one\n\ntwo `lines`\n".into(), mode: Mode::Edit };
        assert_eq!(parse(&to_json(&note)), note);
        let preview = Note { text: String::new(), mode: Mode::Preview };
        assert_eq!(parse(&to_json(&preview)), preview);
    }

    #[test]
    fn corrupt_or_missing_input_falls_back_to_empty_note() {
        assert_eq!(parse("garbage"), Note::default());
        assert_eq!(parse(""), Note::default());
        assert_eq!(parse("{}"), Note::default());
        assert_eq!(parse("{\"text\":123}"), Note::default());
        assert_eq!(parse("{\"text\":\"keep\",\"mode\":7}").text, "keep");
        assert_eq!(Note::default().text, "");
        assert_eq!(Note::default().mode, Mode::Preview);
    }

    #[test]
    fn bom_from_powershell_pipe_is_stripped() {
        let note = Note { text: "hi".into(), mode: Mode::Preview };
        let json = format!("\u{feff}{}", to_json(&note));
        assert_eq!(parse(&json), note);
    }

    #[test]
    fn unknown_mode_falls_back_to_preview() {
        assert_eq!(parse("{\"text\":\"a\",\"mode\":\"bogus\"}").mode, Mode::Preview);
        assert_eq!(parse("{\"text\":\"a\",\"mode\":\"edit\"}").mode, Mode::Edit);
    }

    /// Fresh per-test base dir under the OS temp dir — path logic takes the
    /// base as a parameter precisely so tests never touch the real APPDATA.
    fn temp_base(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("notes-test-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("herdr")).unwrap();
        dir
    }

    fn write_note(path: &Path, text: &str) {
        std::fs::write(path, to_json(&Note { text: text.into(), mode: Mode::Preview })).unwrap();
    }

    #[test]
    fn state_path_keys_on_safe_workspace_ids_only() {
        let base = Path::new("base");
        assert_eq!(
            state_path_in(base, Some("w6")),
            base.join("herdr").join("notes").join("w6.json")
        );
        // Unset (outside herdr) and filename-unsafe ids use the legacy path.
        let legacy = legacy_path_in(base);
        assert_eq!(state_path_in(base, None), legacy);
        for bad in ["", "w6:t1", "../evil", "a b", "-w6", "w6.json"] {
            assert_eq!(state_path_in(base, Some(bad)), legacy, "unsafe id {bad:?}");
        }
    }

    #[test]
    fn note_key_mirrors_file_identity() {
        assert_eq!(note_key(Some("w6")), Some("w6".to_string()));
        // Every id without its own file shares ONE key (None = legacy file).
        assert_eq!(note_key(None), None);
        for bad in ["", "w6:t1", "../evil", "a b", "-w6", "w6.json"] {
            assert_eq!(note_key(Some(bad)), None, "unsafe id {bad:?}");
        }
        // NTFS is case-insensitive: "W6" and "w6" hit the same file on
        // Windows, so their keys (and filenames) must fold together there.
        #[cfg(windows)]
        {
            assert_eq!(note_key(Some("W6")), Some("w6".to_string()));
            let base = Path::new("base");
            assert_eq!(state_path_in(base, Some("W6")), state_path_in(base, Some("w6")));
        }
        #[cfg(not(windows))]
        assert_eq!(note_key(Some("W6")), Some("W6".to_string()));
    }

    #[test]
    fn first_load_moves_the_legacy_note_into_the_workspace_slot() {
        let base = temp_base("migrate");
        write_note(&legacy_path_in(&base), "old note");
        assert_eq!(load_in(&base, Some("w6")).text, "old note");
        assert!(!legacy_path_in(&base).exists(), "legacy file was moved, not copied");
        assert!(state_path_in(&base, Some("w6")).exists());
        // Second load reads the migrated file; nothing left to migrate.
        assert_eq!(load_in(&base, Some("w6")).text, "old note");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn per_workspace_file_wins_over_a_lingering_legacy_file() {
        let base = temp_base("both");
        let ws_path = state_path_in(&base, Some("w6"));
        std::fs::create_dir_all(ws_path.parent().unwrap()).unwrap();
        write_note(&ws_path, "mine");
        write_note(&legacy_path_in(&base), "stale");
        assert_eq!(load_in(&base, Some("w6")).text, "mine");
        assert!(legacy_path_in(&base).exists(), "legacy file untouched when both exist");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn plugin_state_dir_layout_migrates_from_the_config_layout() {
        let base = temp_base("statedir");
        let dir = base.join("plugin-state");
        // Per-workspace file moves over from the config layout on first load.
        let cfg_ws = state_path_in(&base, Some("w6"));
        std::fs::create_dir_all(cfg_ws.parent().unwrap()).unwrap();
        write_note(&cfg_ws, "from config");
        assert_eq!(load_state_dir(&dir, Some(&base), Some("w6")).text, "from config");
        assert!(!cfg_ws.exists(), "moved, not copied");
        assert!(dir.join("w6.json").exists());
        assert_eq!(load_state_dir(&dir, Some(&base), Some("w6")).text, "from config");
        // No workspace id: shared note.json, migrating the config legacy file.
        write_note(&legacy_path_in(&base), "legacy");
        assert_eq!(load_state_dir(&dir, Some(&base), None).text, "legacy");
        assert!(dir.join("note.json").exists());
        // Nothing anywhere is still just an empty note.
        assert_eq!(load_state_dir(&dir, Some(&base), Some("w9")), Note::default());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn unset_workspace_id_reads_the_legacy_file_in_place() {
        let base = temp_base("legacy");
        write_note(&legacy_path_in(&base), "global");
        assert_eq!(load_in(&base, None).text, "global");
        assert!(legacy_path_in(&base).exists(), "no migration without a workspace id");
        let _ = std::fs::remove_dir_all(&base);
        // Nothing on disk at all (any key) is still just an empty note.
        let empty = temp_base("empty");
        assert_eq!(load_in(&empty, Some("w9")), Note::default());
        assert_eq!(load_in(&empty, None), Note::default());
        let _ = std::fs::remove_dir_all(&empty);
    }

    #[test]
    fn global_and_view_files_sit_beside_the_workspace_notes() {
        let base = Path::new("base");
        let cfg = StoreBase::Config(base.to_path_buf());
        assert_eq!(
            shared_path(&cfg, GLOBAL_FILE),
            base.join("herdr").join("notes").join("_global.json")
        );
        assert_eq!(
            shared_path(&cfg, VIEW_FILE).parent(),
            state_path_in(base, Some("w6")).parent()
        );
        let dir = Path::new("state");
        let plugin = StoreBase::PluginState(dir.to_path_buf());
        assert_eq!(shared_path(&plugin, GLOBAL_FILE), dir.join("_global.json"));
        assert_eq!(shared_path(&plugin, VIEW_FILE), dir.join("_view.json"));
        assert_eq!(shared_path(&plugin, GLOBAL_FILE).parent(), state_dir_path(dir, Some("w6")).parent());
    }

    #[test]
    fn global_note_is_independent_of_the_workspace_and_never_migrates() {
        let base = temp_base("global");
        let dir = base.join("plugin-state");
        write_note(&legacy_path_in(&base), "legacy");
        let store = Store {
            base: StoreBase::PluginState(dir.clone()),
            config: Some(base.clone()),
            workspace: Some("w6".into()),
        };
        assert_eq!(store.load(Tab::Global), Note::default());
        assert!(legacy_path_in(&base).exists(), "loading the global note leaves legacy files");
        store.save(Tab::Global, &Note { text: "shared".into(), mode: Mode::Edit });
        assert_eq!(store.load(Tab::Global), Note { text: "shared".into(), mode: Mode::Edit });
        assert!(dir.join("_global.json").exists());
        assert_eq!(store.load(Tab::Workspace).text, "legacy", "workspace note is its own file");
        assert!(store.mtime(Tab::Global).is_some());
        assert!(store.mtime(Tab::Workspace).is_some());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn view_roundtrips_and_garbage_means_workspace() {
        for tab in [Tab::Workspace, Tab::Global] {
            assert_eq!(parse_tab(&tab_to_json(tab)), tab);
        }
        assert_eq!(tab_to_json(Tab::Global), r#"{"tab":"global"}"#);
        assert_eq!(parse_tab(&format!("\u{feff}{}", tab_to_json(Tab::Global))), Tab::Global);
        for bad in ["", "garbage", "{}", "[]", r#"{"tab":5}"#, r#"{"tab":"bogus"}"#, "null"] {
            assert_eq!(parse_tab(bad), Tab::Workspace, "input {bad:?}");
        }
    }

    #[test]
    fn missing_view_file_opens_the_workspace_tab_and_save_tab_persists() {
        let base = temp_base("view");
        let store = Store::in_plugin_dir(base.join("state"), "w6");
        assert_eq!(store.load_tab(), Tab::Workspace);
        store.save_tab(Tab::Global);
        assert_eq!(store.load_tab(), Tab::Global);
        let _ = std::fs::remove_dir_all(&base);
    }
}
