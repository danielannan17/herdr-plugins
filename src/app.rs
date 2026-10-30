//! Notes TUI: preview/edit modes over one scrollable markdown note — this
//! workspace's or the global one, chosen by tab — autosaved to its note file
//! (see state.rs), heartbeating a pane identity token so the launcher can
//! toggle / focus / replace the pane.
//!
//! There is no manual save workflow — everything autosaves — and the only
//! destructive action (`x`, clear the note) sits behind a y/N confirm.

use std::time::{Duration, Instant, SystemTime};

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, Clear, Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState,
};
use unicode_width::UnicodeWidthChar;

use crate::markdown::render_markdown;
use crate::state::{Mode, Note, Store, Tab};

/// Debounce for the edit-mode autosave.
const AUTOSAVE_AFTER: Duration = Duration::from_secs(2);
/// Identity re-stamp interval (launcher stale threshold is 20s).
const HEARTBEAT_EVERY: Duration = Duration::from_secs(5);
/// How often the active note's file is checked for outside changes.
const RELOAD_CHECK_EVERY: Duration = Duration::from_secs(1);

/// Shown in preview when the note is empty; doubles as the quick-start help.
const EMPTY_HELP: &str = "(empty note)\n\n  e or Enter        start writing\n  Esc               back to preview (saves)\n  Tab or 1/2        switch Workspace / Global note\n  Up/Dn PgUp/PgDn   scroll, g/G top/bottom\n  x                 clear the note (asks first)\n  q                 quit\n\nEverything autosaves and survives restarts.";

pub struct App {
    tab: Tab,
    note: Note,
    /// The note split into lines while editing.
    lines: Vec<String>,
    row: usize,
    col: usize,
    edit_scroll: usize,
    preview_scroll: usize,
    confirm_clear: bool,
    dirty: bool,
    last_edit: Instant,
    /// Body height from the last draw, for PgUp/PgDn and scroll clamping.
    body_height: usize,
    pane_id: Option<String>,
    last_beat: Instant,
    /// `None` in most unit tests so exercising keys never touches disk.
    store: Option<Store>,
    /// Modification time of the active note's file when this pane last read
    /// or wrote it.
    known_mtime: Option<SystemTime>,
    last_check: Instant,
}

impl App {
    pub fn new() -> Self {
        let store = Store::from_env();
        let tab = store.as_ref().map(Store::load_tab).unwrap_or_default();
        // Stat before reading: a write landing in between is picked up by the
        // next reload check instead of being missed.
        let known_mtime = store.as_ref().and_then(|s| s.mtime(tab));
        let note = store.as_ref().map(|s| s.load(tab)).unwrap_or_default();
        let mut app = Self::with_note(note, store);
        app.tab = tab;
        app.known_mtime = known_mtime;
        app.pane_id = std::env::var("HERDR_PANE_ID").ok().filter(|id| !id.is_empty());
        app.report_tokens();
        app
    }

    fn with_note(note: Note, store: Option<Store>) -> Self {
        let mut app = Self {
            tab: Tab::default(),
            note,
            lines: Vec::new(),
            row: 0,
            col: 0,
            edit_scroll: 0,
            preview_scroll: 0,
            confirm_clear: false,
            dirty: false,
            last_edit: Instant::now(),
            body_height: 20,
            pane_id: None,
            last_beat: Instant::now(),
            store,
            known_mtime: None,
            last_check: Instant::now(),
        };
        if app.note.mode == Mode::Edit {
            app.enter_edit();
        }
        app
    }

    // ----- persistence & heartbeat -------------------------------------

    fn save(&mut self) {
        if let Some(store) = &self.store {
            store.save(self.tab, &self.note);
            self.known_mtime = store.mtime(self.tab);
        }
    }

    /// Reload the active note when something else rewrote its file (another
    /// pane on the same note, a script) and nothing here is unsaved. With
    /// unsaved edits the next autosave wins.
    pub fn maybe_reload(&mut self) {
        if self.last_check.elapsed() < RELOAD_CHECK_EVERY {
            return;
        }
        self.last_check = Instant::now();
        let Some(store) = &self.store else { return };
        let disk = store.mtime(self.tab);
        if !should_reload(self.dirty, disk, self.known_mtime) {
            return;
        }
        self.known_mtime = disk;
        self.note.text = store.load(self.tab).text;
        if self.note.mode == Mode::Edit {
            self.lines = self.note.text.split('\n').map(String::from).collect();
            self.row = self.row.min(self.lines.len() - 1);
            self.col = self.col.min(clen(&self.lines[self.row]));
        }
    }

    /// Show the other note: save this one, remember the tab, then load the
    /// target note with its saved mode.
    fn switch_tab(&mut self, tab: Tab) {
        if tab == self.tab {
            return;
        }
        self.commit();
        self.dirty = false;
        self.save();
        self.tab = tab;
        self.confirm_clear = false;
        self.preview_scroll = 0;
        self.edit_scroll = 0;
        let Some(store) = &self.store else { return };
        store.save_tab(tab);
        self.known_mtime = store.mtime(tab);
        self.note = store.load(tab);
        if self.note.mode == Mode::Edit {
            self.enter_edit();
        }
    }

    /// Copy the edit buffer back into the note.
    fn commit(&mut self) {
        if self.note.mode == Mode::Edit {
            self.note.text = self.lines.join("\n");
        }
    }

    /// Debounced autosave: flush ~2s after the last edit keystroke.
    pub fn maybe_flush(&mut self) {
        if self.dirty && self.last_edit.elapsed() >= AUTOSAVE_AFTER {
            self.commit();
            self.save();
            self.dirty = false;
        }
    }

    /// Final save on the way out.
    pub fn finalize(&mut self) {
        self.commit();
        self.save();
    }

    /// Re-stamp the identity token so the launcher knows this pane is alive.
    /// Cheap (one socket round-trip); the event loop calls this every few
    /// seconds. Silently a no-op outside herdr.
    pub fn heartbeat(&mut self) {
        if self.last_beat.elapsed() < HEARTBEAT_EVERY {
            return;
        }
        self.last_beat = Instant::now();
        self.report_tokens();
    }

    fn report_tokens(&self) {
        let Some(pane_id) = &self.pane_id else { return };
        let _ = crate::ipc::stamp_identity(pane_id);
    }

    // ----- keys --------------------------------------------------------

    /// Returns true when the app should quit. Esc NEVER quits — it closes the
    /// confirm overlay or leaves edit mode at most.
    pub fn on_key(&mut self, key: KeyEvent) -> bool {
        if key.kind != KeyEventKind::Press {
            return false;
        }
        if self.confirm_clear {
            if matches!(key.code, KeyCode::Char('y') | KeyCode::Char('Y')) {
                self.note.text.clear();
                self.preview_scroll = 0;
                self.save();
            }
            self.confirm_clear = false;
            return false;
        }
        match self.note.mode {
            Mode::Preview => self.on_key_preview(key),
            Mode::Edit => {
                self.on_key_edit(key);
                false
            }
        }
    }

    fn on_key_preview(&mut self, key: KeyEvent) -> bool {
        let page = self.body_height.max(1);
        match key.code {
            KeyCode::Char('q') => {
                self.save();
                return true;
            }
            KeyCode::Char('e') | KeyCode::Enter => self.enter_edit(),
            KeyCode::Tab => self.switch_tab(match self.tab {
                Tab::Workspace => Tab::Global,
                Tab::Global => Tab::Workspace,
            }),
            KeyCode::Char('1') => self.switch_tab(Tab::Workspace),
            KeyCode::Char('2') => self.switch_tab(Tab::Global),
            KeyCode::Up => self.preview_scroll = self.preview_scroll.saturating_sub(1),
            KeyCode::Down => self.preview_scroll = self.preview_scroll.saturating_add(1),
            KeyCode::PageUp => self.preview_scroll = self.preview_scroll.saturating_sub(page),
            KeyCode::PageDown => self.preview_scroll = self.preview_scroll.saturating_add(page),
            // g/G because herdr `pane send-keys` rejects Home/End.
            KeyCode::Home | KeyCode::Char('g') => self.preview_scroll = 0,
            KeyCode::End | KeyCode::Char('G') => self.preview_scroll = usize::MAX, // clamped in draw
            KeyCode::Char('x') => self.confirm_clear = true,
            _ => {}
        }
        false
    }

    fn enter_edit(&mut self) {
        self.lines = self.note.text.split('\n').map(String::from).collect();
        self.row = 0;
        self.col = 0;
        self.edit_scroll = 0;
        self.note.mode = Mode::Edit;
    }

    fn leave_edit(&mut self) {
        self.commit();
        self.note.mode = Mode::Preview;
        self.dirty = false;
        self.save();
    }

    fn touch(&mut self) {
        self.dirty = true;
        self.last_edit = Instant::now();
    }

    fn on_key_edit(&mut self, key: KeyEvent) {
        // AltGr arrives from Windows as CONTROL|ALT on a plain character
        // (@ { [ ] } \ on German/French/Nordic layouts) — that is text to
        // insert, not a Ctrl shortcut.
        let altgr = key.modifiers.contains(KeyModifiers::CONTROL)
            && key.modifiers.contains(KeyModifiers::ALT);
        if key.modifiers.contains(KeyModifiers::CONTROL) && !altgr {
            if matches!(key.code, KeyCode::Char('s') | KeyCode::Char('S')) {
                self.commit();
                self.save();
                self.dirty = false;
            }
            return;
        }
        let line_len = clen(&self.lines[self.row]);
        match key.code {
            KeyCode::Esc => self.leave_edit(),
            KeyCode::Left => {
                if self.col > 0 {
                    self.col -= 1;
                } else if self.row > 0 {
                    self.row -= 1;
                    self.col = clen(&self.lines[self.row]);
                }
            }
            KeyCode::Right => {
                if self.col < line_len {
                    self.col += 1;
                } else if self.row + 1 < self.lines.len() {
                    self.row += 1;
                    self.col = 0;
                }
            }
            KeyCode::Up => {
                if self.row > 0 {
                    self.row -= 1;
                    self.col = self.col.min(clen(&self.lines[self.row]));
                }
            }
            KeyCode::Down => {
                if self.row + 1 < self.lines.len() {
                    self.row += 1;
                    self.col = self.col.min(clen(&self.lines[self.row]));
                }
            }
            KeyCode::Home => self.col = 0,
            KeyCode::End => self.col = line_len,
            KeyCode::PageUp => {
                self.row = self.row.saturating_sub(self.body_height.max(1));
                self.col = self.col.min(clen(&self.lines[self.row]));
            }
            KeyCode::PageDown => {
                self.row = (self.row + self.body_height.max(1)).min(self.lines.len() - 1);
                self.col = self.col.min(clen(&self.lines[self.row]));
            }
            KeyCode::Enter => {
                let at = byte_idx(&self.lines[self.row], self.col);
                let rest = self.lines[self.row].split_off(at);
                self.lines.insert(self.row + 1, rest);
                self.row += 1;
                self.col = 0;
                self.touch();
            }
            KeyCode::Backspace => {
                if self.col > 0 {
                    let at = byte_idx(&self.lines[self.row], self.col - 1);
                    self.lines[self.row].remove(at);
                    self.col -= 1;
                    self.touch();
                } else if self.row > 0 {
                    let tail = self.lines.remove(self.row);
                    self.row -= 1;
                    self.col = clen(&self.lines[self.row]);
                    self.lines[self.row].push_str(&tail);
                    self.touch();
                }
            }
            KeyCode::Delete => {
                if self.col < line_len {
                    let at = byte_idx(&self.lines[self.row], self.col);
                    self.lines[self.row].remove(at);
                    self.touch();
                } else if self.row + 1 < self.lines.len() {
                    let tail = self.lines.remove(self.row + 1);
                    self.lines[self.row].push_str(&tail);
                    self.touch();
                }
            }
            KeyCode::Tab => {
                let at = byte_idx(&self.lines[self.row], self.col);
                self.lines[self.row].insert_str(at, "  ");
                self.col += 2;
                self.touch();
            }
            KeyCode::Char(c) => {
                let at = byte_idx(&self.lines[self.row], self.col);
                self.lines[self.row].insert(at, c);
                self.col += 1;
                self.touch();
            }
            _ => {}
        }
    }

    // ----- drawing -----------------------------------------------------

    pub fn draw(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let [title_a, body_a, hint_a] =
            Layout::vertical([Constraint::Length(1), Constraint::Min(1), Constraint::Length(1)])
                .areas(area);
        self.body_height = usize::from(body_a.height);

        // Body first: the preview reports a scroll hint for the title line.
        let (mode, scroll_hint) = match self.note.mode {
            Mode::Preview => ("preview", self.draw_preview(frame, body_a)),
            Mode::Edit => {
                self.draw_edit(frame, body_a);
                ("edit", None)
            }
        };

        // The pane border already says "Notes" (metadata title) — repeating it
        // here read as a duplicate, so the header carries only the tabs, mode
        // and scroll.
        let tab_style = |tab: Tab| {
            if self.tab == tab {
                Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD)
            } else {
                Style::default().add_modifier(Modifier::DIM)
            }
        };
        let mut title = vec![
            Span::styled(" Workspace ", tab_style(Tab::Workspace)),
            Span::styled(" Global ", tab_style(Tab::Global)),
            Span::styled(format!(" [{mode}]"), Style::default().fg(Color::Cyan)),
        ];
        if let Some(hint) = scroll_hint {
            title.push(Span::styled(
                format!("  {hint}"),
                Style::default().add_modifier(Modifier::DIM),
            ));
        }
        frame.render_widget(Paragraph::new(Line::from(title)), title_a);

        let hints = match self.note.mode {
            Mode::Preview => {
                " e/Enter edit   Tab/1/2 switch   Up/Dn scroll   g/G top/end   x clear   q quit"
            }
            Mode::Edit => " Esc preview (saves, then Tab/1/2 switch)   Ctrl+S save",
        };
        frame.render_widget(
            Paragraph::new(Span::styled(hints, Style::default().add_modifier(Modifier::DIM))),
            hint_a,
        );

        if self.confirm_clear {
            draw_confirm(frame, area);
        }
    }

    /// Renders the preview body; returns a "top-line/total" scroll hint when
    /// the content overflows the pane.
    fn draw_preview(&mut self, frame: &mut Frame, area: Rect) -> Option<String> {
        if self.note.text.trim().is_empty() {
            self.preview_scroll = 0;
            frame.render_widget(
                Paragraph::new(EMPTY_HELP).style(Style::default().add_modifier(Modifier::DIM)),
                area,
            );
            return None;
        }
        // The rightmost column is reserved for the overflow scrollbar so text
        // never sits underneath it.
        let text_w = usize::from(area.width).saturating_sub(1).max(1);
        let lines = render_markdown(&self.note.text, text_w);
        let total = lines.len();
        let max = total.saturating_sub(usize::from(area.height));
        self.preview_scroll = self.preview_scroll.min(max);
        let scroll = u16::try_from(self.preview_scroll).unwrap_or(u16::MAX);
        frame.render_widget(Paragraph::new(lines).scroll((scroll, 0)), area);
        draw_scrollbar(frame, area, max, self.preview_scroll);
        (max > 0).then(|| format!("{}/{total}", self.preview_scroll + 1))
    }

    fn draw_edit(&mut self, frame: &mut Frame, area: Rect) {
        let h = usize::from(area.height).max(1);
        // Rightmost column reserved for the overflow scrollbar.
        let w = usize::from(area.width).saturating_sub(1).max(2);
        if self.row < self.edit_scroll {
            self.edit_scroll = self.row;
        }
        if self.row >= self.edit_scroll + h {
            self.edit_scroll = self.row + 1 - h;
        }
        // Horizontal shift keeps the cursor visible on overlong lines. The
        // shift is found in display COLUMNS (wide CJK/emoji chars count 2),
        // otherwise the REVERSED cursor cell could sit past the pane edge.
        let widths: Vec<usize> =
            self.lines[self.row].chars().map(|c| c.width().unwrap_or(0)).collect();
        let cursor_w = widths.get(self.col).copied().unwrap_or(1).max(1);
        let mut h_off = 0;
        let mut visible: usize = widths[..self.col].iter().sum();
        while visible + cursor_w > w && h_off < self.col {
            visible -= widths[h_off];
            h_off += 1;
        }
        let mut lines: Vec<Line> = Vec::new();
        for (i, line) in self.lines.iter().enumerate().skip(self.edit_scroll).take(h) {
            let chars: Vec<char> = line.chars().skip(h_off).collect();
            if i == self.row {
                let col = self.col - h_off;
                let before: String = chars.iter().take(col).collect();
                let at: String = chars.get(col).map_or(" ".to_string(), |c| c.to_string());
                let after: String = chars.iter().skip(col + 1).collect();
                lines.push(Line::from(vec![
                    Span::raw(before),
                    Span::styled(at, Style::default().add_modifier(Modifier::REVERSED)),
                    Span::raw(after),
                ]));
            } else {
                lines.push(Line::from(chars.into_iter().collect::<String>()));
            }
        }
        frame.render_widget(Paragraph::new(lines), area);
        draw_scrollbar(frame, area, self.lines.len().saturating_sub(h), self.edit_scroll);
    }
}

/// Vertical scrollbar on the right edge; hidden when everything fits.
fn draw_scrollbar(frame: &mut Frame, area: Rect, max_scroll: usize, position: usize) {
    if max_scroll == 0 {
        return;
    }
    let mut state = ScrollbarState::new(max_scroll).position(position.min(max_scroll));
    frame.render_stateful_widget(
        Scrollbar::new(ScrollbarOrientation::VerticalRight),
        area,
        &mut state,
    );
}

fn draw_confirm(frame: &mut Frame, area: Rect) {
    let w = 30.min(area.width);
    let h = 3.min(area.height);
    let rect = Rect {
        x: area.x + (area.width - w) / 2,
        y: area.y + (area.height - h) / 2,
        width: w,
        height: h,
    };
    frame.render_widget(Clear, rect);
    frame.render_widget(
        Paragraph::new(" Clear the note? y/N").block(Block::bordered().title(" Clear ")),
        rect,
    );
}

/// A file changed by someone else is reloaded only while nothing here is
/// unsaved (`dirty` covers an edit buffer with unsaved changes). A missing
/// file never triggers a reload.
fn should_reload(dirty: bool, disk: Option<SystemTime>, known: Option<SystemTime>) -> bool {
    !dirty && disk.is_some() && disk != known
}

fn clen(s: &str) -> usize {
    s.chars().count()
}

fn byte_idx(s: &str, char_idx: usize) -> usize {
    s.char_indices().nth(char_idx).map_or(s.len(), |(b, _)| b)
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn app(text: &str) -> App {
        App::with_note(
            Note { text: text.to_string(), mode: Mode::Preview },
            None, // never touch the real state files from tests
        )
    }

    #[test]
    fn edit_roundtrip_insert_newline_backspace() {
        let mut a = app("ab");
        a.on_key(key(KeyCode::Enter)); // enter edit mode
        assert_eq!(a.note.mode, Mode::Edit);
        a.on_key(key(KeyCode::End));
        a.on_key(key(KeyCode::Char('c')));
        a.on_key(key(KeyCode::Enter)); // newline
        a.on_key(key(KeyCode::Char('d')));
        a.on_key(key(KeyCode::Esc)); // back to preview, committing
        assert_eq!(a.note.mode, Mode::Preview);
        assert_eq!(a.note.text, "abc\nd");

        a.on_key(key(KeyCode::Char('e')));
        a.on_key(key(KeyCode::Down));
        a.on_key(key(KeyCode::End));
        a.on_key(key(KeyCode::Backspace)); // delete 'd'
        a.on_key(key(KeyCode::Backspace)); // join lines
        a.on_key(key(KeyCode::Esc));
        assert_eq!(a.note.text, "abc");
    }

    #[test]
    fn esc_never_quits_and_q_quits_only_in_preview() {
        let mut a = app("x");
        assert!(!a.on_key(key(KeyCode::Esc)), "Esc in preview must not quit");
        a.on_key(key(KeyCode::Char('e')));
        assert!(!a.on_key(key(KeyCode::Esc)), "Esc in edit leaves edit, not the app");
        assert_eq!(a.note.mode, Mode::Preview);
        // 'q' typed while editing is just a character.
        a.on_key(key(KeyCode::Char('e')));
        assert!(!a.on_key(key(KeyCode::Char('q'))));
        a.on_key(key(KeyCode::Esc));
        assert_eq!(a.note.text, "qx");
        assert!(a.on_key(key(KeyCode::Char('q'))), "q in preview quits");
    }

    #[test]
    fn clear_requires_confirmation() {
        let mut a = app("keep me");
        a.on_key(key(KeyCode::Char('x')));
        assert!(a.confirm_clear);
        assert!(!a.on_key(key(KeyCode::Esc)), "Esc closes the overlay, not the app");
        assert_eq!(a.note.text, "keep me");
        a.on_key(key(KeyCode::Char('x')));
        a.on_key(key(KeyCode::Char('n')));
        assert_eq!(a.note.text, "keep me", "declined clear keeps the note");
        assert!(!a.confirm_clear);
        a.on_key(key(KeyCode::Char('x')));
        a.on_key(key(KeyCode::Char('y')));
        assert_eq!(a.note.text, "", "confirmed clear empties the note");
    }

    #[test]
    fn preview_scroll_keys_move_and_clamp_at_top() {
        let mut a = app("line\nline\nline");
        a.on_key(key(KeyCode::Up));
        assert_eq!(a.preview_scroll, 0, "scrolling above the top clamps");
        a.on_key(key(KeyCode::Down));
        a.on_key(key(KeyCode::Down));
        assert_eq!(a.preview_scroll, 2);
        a.on_key(key(KeyCode::Char('g')));
        assert_eq!(a.preview_scroll, 0);
        a.on_key(key(KeyCode::Char('G')));
        assert_eq!(a.preview_scroll, usize::MAX, "jump to end; draw clamps to content");
    }

    #[test]
    fn altgr_chars_insert_but_ctrl_shortcuts_do_not() {
        let mut a = app("");
        a.on_key(key(KeyCode::Char('e')));
        // AltGr = CONTROL|ALT on Windows: a printable char, must insert.
        a.on_key(KeyEvent::new(
            KeyCode::Char('@'),
            KeyModifiers::CONTROL | KeyModifiers::ALT,
        ));
        // Plain Ctrl+char stays a shortcut, never inserts.
        a.on_key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::CONTROL));
        a.on_key(key(KeyCode::Esc));
        assert_eq!(a.note.text, "@");
    }

    #[test]
    fn debounced_autosave_commits_the_buffer() {
        let mut a = app("");
        a.on_key(key(KeyCode::Char('e')));
        a.on_key(key(KeyCode::Char('z')));
        assert!(a.dirty);
        a.last_edit = Instant::now() - AUTOSAVE_AFTER;
        a.maybe_flush();
        assert!(!a.dirty);
        assert_eq!(a.note.text, "z", "flush committed the edit buffer");
    }

    #[test]
    fn startup_in_edit_mode_loads_the_buffer() {
        let mut a = App::with_note(Note { text: "a\nb".into(), mode: Mode::Edit }, None);
        assert_eq!(a.lines, vec!["a".to_string(), "b".to_string()]);
        a.on_key(key(KeyCode::Esc));
        assert_eq!(a.note.text, "a\nb", "leaving edit commits losslessly");
    }

    /// App on a fresh temp plugin-state dir, as the real pane runs.
    fn stored_app(tag: &str, text: &str) -> (App, PathBuf) {
        let dir = std::env::temp_dir().join(format!("notes-app-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let store = Store::in_plugin_dir(dir.clone(), "w1");
        let a = App::with_note(Note { text: text.into(), mode: Mode::Preview }, Some(store));
        (a, dir)
    }

    fn disk(dir: &std::path::Path, tab: Tab) -> Note {
        Store::in_plugin_dir(dir.to_path_buf(), "w1").load(tab)
    }

    #[test]
    fn tab_keys_switch_notes_in_preview() {
        let (mut a, dir) = stored_app("keys", "ws");
        assert_eq!(a.tab, Tab::Workspace);
        a.on_key(key(KeyCode::Tab));
        assert_eq!(a.tab, Tab::Global);
        a.on_key(key(KeyCode::Tab));
        assert_eq!(a.tab, Tab::Workspace);
        a.on_key(key(KeyCode::Char('2')));
        assert_eq!(a.tab, Tab::Global);
        a.on_key(key(KeyCode::Char('2')));
        assert_eq!(a.tab, Tab::Global, "selecting the active tab keeps it");
        a.on_key(key(KeyCode::Char('1')));
        assert_eq!(a.tab, Tab::Workspace);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tab_keys_are_text_in_edit_mode() {
        let (mut a, dir) = stored_app("edit", "");
        a.on_key(key(KeyCode::Char('e')));
        for code in [KeyCode::Tab, KeyCode::Char('1'), KeyCode::Char('2')] {
            a.on_key(key(code));
        }
        assert_eq!(a.tab, Tab::Workspace);
        a.on_key(key(KeyCode::Esc));
        assert_eq!(a.note.text, "  12");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn switching_saves_this_note_and_shows_the_other() {
        let (mut a, dir) = stored_app("switch", "workspace text");
        Store::in_plugin_dir(dir.clone(), "w1")
            .save(Tab::Global, &Note { text: "global text".into(), mode: Mode::Preview });
        a.preview_scroll = 5;
        a.on_key(key(KeyCode::Char('2')));
        assert_eq!(disk(&dir, Tab::Workspace).text, "workspace text", "saved on the way out");
        assert_eq!(a.note.text, "global text");
        assert_eq!(a.preview_scroll, 0);
        assert!(!a.confirm_clear);
        assert_eq!(Store::in_plugin_dir(dir.clone(), "w1").load_tab(), Tab::Global);

        a.on_key(key(KeyCode::Char('x')));
        a.on_key(key(KeyCode::Char('y')));
        assert_eq!(disk(&dir, Tab::Global).text, "", "clear acts on the active note only");
        a.on_key(key(KeyCode::Char('1')));
        assert_eq!(a.note.text, "workspace text");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn switching_restores_the_other_notes_saved_mode() {
        let (mut a, dir) = stored_app("mode", "ws");
        Store::in_plugin_dir(dir.clone(), "w1")
            .save(Tab::Global, &Note { text: "a\nb".into(), mode: Mode::Edit });
        a.on_key(key(KeyCode::Char('2')));
        assert_eq!(a.note.mode, Mode::Edit);
        assert_eq!(a.lines, vec!["a".to_string(), "b".to_string()]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn reload_decision_waits_for_a_clean_note_and_an_existing_changed_file() {
        let t0 = SystemTime::UNIX_EPOCH;
        let t1 = t0 + Duration::from_secs(1);
        assert!(should_reload(false, Some(t1), Some(t0)));
        assert!(should_reload(false, Some(t1), None), "file appeared");
        assert!(!should_reload(true, Some(t1), Some(t0)), "unsaved edits win");
        assert!(!should_reload(false, Some(t0), Some(t0)), "unchanged");
        assert!(!should_reload(false, None, Some(t0)), "missing file is never reloaded");
        assert!(!should_reload(false, None, None));
    }

    #[test]
    fn external_change_reloads_a_clean_note_but_not_unsaved_edits() {
        let (mut a, dir) = stored_app("reload", "mine");
        a.save();
        let store = Store::in_plugin_dir(dir.clone(), "w1");
        let external = |text: &str| {
            store.save(Tab::Workspace, &Note { text: text.into(), mode: Mode::Preview });
            let f = std::fs::File::options().write(true).open(dir.join("w1.json")).unwrap();
            f.set_modified(SystemTime::now() + Duration::from_secs(60)).unwrap();
        };
        let due = |a: &mut App| a.last_check = Instant::now() - RELOAD_CHECK_EVERY;

        external("theirs");
        a.last_check = Instant::now();
        a.maybe_reload();
        assert_eq!(a.note.text, "mine", "checked at most once a second");
        due(&mut a);
        a.maybe_reload();
        assert_eq!(a.note.text, "theirs");

        a.on_key(key(KeyCode::Char('e')));
        a.on_key(key(KeyCode::Char('!')));
        external("later");
        due(&mut a);
        a.maybe_reload();
        a.on_key(key(KeyCode::Esc));
        assert_eq!(a.note.text, "!theirs", "mid-edit changes are kept; autosave wins");
        assert_eq!(disk(&dir, Tab::Workspace).text, "!theirs");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tab_bar_draws_in_tiny_panes_without_panicking() {
        use ratatui::Terminal;
        use ratatui::backend::TestBackend;
        let mut a = app("# t\n\nbody");
        for (w, h) in [(80, 10), (29, 5), (10, 3), (3, 1), (1, 1), (20, 0)] {
            let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
            term.draw(|f| a.draw(f)).unwrap();
        }
        let mut term = Terminal::new(TestBackend::new(40, 4)).unwrap();
        term.draw(|f| a.draw(f)).unwrap();
        let top: String = (0..40).map(|x| term.backend().buffer()[(x, 0)].symbol().to_string()).collect();
        assert!(top.starts_with(" Workspace  Global  [preview]"), "{top:?}");
    }
}
