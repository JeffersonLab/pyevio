//! Drawing the screens with ratatui. This is the only module that knows
//! about widgets; it reads the [`App`] state, tells each list how many rows
//! fit, and renders only the visible rows.

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Line;
use ratatui::widgets::{Block, Cell, Paragraph, Row, Table, TableState, Wrap};
use ratatui::Frame;

use crate::format::{
    event_cells, file_line, hex_lines, record_cells, record_header_lines, status_line, tree_cells,
    EVENT_COLUMNS, HELP_LINES, RECORD_COLUMNS, TREE_COLUMNS,
};
use crate::state::{App, ListCursor, Screen};

/// Terminals narrower than this draw the records table without the header
/// side panel.
const SIDE_PANEL_MIN_WIDTH: u16 = 100;

/// Draw the current screen.
pub fn draw(frame: &mut Frame, app: &mut App) {
    let [title, body, status, hint] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(3),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .areas(frame.area());

    let screen_name = match app.screen {
        Screen::Records => "records",
        Screen::Events => "events",
        Screen::Event => "event",
        Screen::Help => "help",
    };
    let title_line = Line::from(format!(
        "evio-tui  {}  [{screen_name}]",
        app.path().display()
    ))
    .style(Style::new().add_modifier(Modifier::BOLD));
    frame.render_widget(Paragraph::new(title_line), title);

    match app.screen {
        Screen::Records => draw_records(frame, app, body),
        Screen::Events => draw_events(frame, app, body),
        Screen::Event => draw_event(frame, app, body),
        Screen::Help => draw_help(frame, body),
    }

    let status_text = match &app.message {
        Some(message) => Line::from(message.clone()).style(Style::new().fg(Color::Red)),
        None => Line::from(status_line(&app.scan)),
    };
    frame.render_widget(Paragraph::new(status_text), status);

    let keys = match app.screen {
        Screen::Records => "Enter open record  arrows/PgUp/PgDn/Home/End/g/G move  ? help  q quit",
        Screen::Events => "Enter open event  Esc back  arrows move  ? help  q quit",
        Screen::Event => "n/p words  f frame summary  Esc back  arrows move  ? help  q quit",
        Screen::Help => "Esc or ? back  q quit",
    };
    frame.render_widget(
        Paragraph::new(Line::from(keys).style(Style::new().fg(Color::DarkGray))),
        hint,
    );
}

/// Rows of a bordered table with a header row.
fn table_rows_available(area: Rect) -> usize {
    area.height.saturating_sub(3) as usize
}

/// Render the visible window of a list as a table.
fn render_table(
    frame: &mut Frame,
    area: Rect,
    title: String,
    columns: &[&str],
    widths: &[Constraint],
    cursor: &ListCursor,
    rows: Vec<Row<'static>>,
) {
    let header = Row::new(columns.iter().map(|c| Cell::from(*c)))
        .style(Style::new().add_modifier(Modifier::UNDERLINED));
    let table = Table::new(rows, widths)
        .header(header)
        .block(Block::bordered().title(title))
        .row_highlight_style(Style::new().add_modifier(Modifier::REVERSED));
    let mut state = TableState::new().with_selected(cursor.selected_in_view());
    frame.render_stateful_widget(table, area, &mut state);
}

fn cells(values: &[String]) -> Vec<Cell<'static>> {
    values.iter().cloned().map(Cell::from).collect()
}

fn draw_records(frame: &mut Frame, app: &mut App, body: Rect) {
    let (table_area, panel) = if body.width >= SIDE_PANEL_MIN_WIDTH {
        let [left, right] =
            Layout::horizontal([Constraint::Min(60), Constraint::Length(50)]).areas(body);
        (left, Some(right))
    } else {
        (body, None)
    };
    app.records.set_height(table_rows_available(table_area));
    let visible = app.records.visible();
    let rows: Vec<Row<'static>> = app.scan.records[visible]
        .iter()
        .map(|s| {
            let row = Row::new(cells(&record_cells(s)));
            if s.error_count > 0 || s.is_truncated {
                row.style(Style::new().fg(Color::Red))
            } else {
                row
            }
        })
        .collect();
    let widths = [
        Constraint::Length(7),
        Constraint::Length(13),
        Constraint::Length(10),
        Constraint::Length(7),
        Constraint::Length(7),
        Constraint::Length(7),
        Constraint::Length(8),
        Constraint::Length(5),
        Constraint::Length(6),
    ];
    let title = format!(" {} ", app.scan.records_label());
    render_table(
        frame,
        table_area,
        title,
        &RECORD_COLUMNS,
        &widths,
        &app.records,
        rows,
    );

    if let Some(panel) = panel {
        let mut lines = vec![file_line(app.scan.start.as_ref()), String::new()];
        match app.selected_record() {
            Some(s) => lines.extend(record_header_lines(s)),
            None => lines.push("no record yet".into()),
        }
        let text: Vec<Line> = lines.into_iter().map(Line::from).collect();
        frame.render_widget(
            Paragraph::new(text)
                .block(Block::bordered().title(" header "))
                .wrap(Wrap { trim: false }),
            panel,
        );
    }
}

fn draw_events(frame: &mut Frame, app: &mut App, body: Rect) {
    let Some(loaded) = &app.loaded else {
        frame.render_widget(
            Paragraph::new("no record loaded").block(Block::bordered()),
            body,
        );
        return;
    };
    app.events.set_height(table_rows_available(body));
    let visible = app.events.visible();
    let rows: Vec<Row<'static>> = loaded.events[visible]
        .iter()
        .map(|e| {
            let row = Row::new(cells(&event_cells(e)));
            if !e.header_ok {
                row.style(Style::new().fg(Color::Red))
            } else {
                row
            }
        })
        .collect();
    let widths = [
        Constraint::Length(7),
        Constraint::Length(10),
        Constraint::Length(8),
        Constraint::Length(18),
        Constraint::Length(5),
        Constraint::Min(14),
    ];
    let title = format!(
        " record {} at 0x{:X}: {} events, {} bytes{} ",
        loaded.index,
        loaded.offset,
        loaded.events.len(),
        loaded.record.present_bytes,
        if loaded.record.errors.is_empty() {
            String::new()
        } else {
            format!(", {} errors", loaded.record.errors.len())
        }
    );
    let cursor = app.events;
    render_table(frame, body, title, &EVENT_COLUMNS, &widths, &cursor, rows);
}

fn draw_event(frame: &mut Frame, app: &mut App, body: Rect) {
    let (Some(loaded), Some(view)) = (&app.loaded, &app.view) else {
        frame.render_widget(
            Paragraph::new("no event opened").block(Block::bordered()),
            body,
        );
        return;
    };
    let [tree_area, panel] =
        Layout::vertical([Constraint::Percentage(55), Constraint::Min(6)]).areas(body);
    app.tree.set_height(table_rows_available(tree_area));
    let visible = app.tree.visible();
    let rows: Vec<Row<'static>> = view.rows[visible]
        .iter()
        .map(|r| {
            let (values, is_error) = tree_cells(r);
            let row = Row::new(cells(&values));
            if is_error {
                row.style(Style::new().fg(Color::Red))
            } else {
                row
            }
        })
        .collect();
    let widths = [
        Constraint::Min(24),
        Constraint::Length(8),
        Constraint::Length(18),
        Constraint::Length(5),
        Constraint::Length(10),
        Constraint::Length(8),
    ];
    let event_words = loaded
        .record
        .event(view.event_index)
        .map(|w| w.len())
        .unwrap_or(0);
    let title = format!(
        " record {} event {}: {} words, {} structures ",
        loaded.index,
        view.event_index,
        event_words,
        view.rows.len()
    );
    let cursor = app.tree;
    render_table(
        frame,
        tree_area,
        title,
        &TREE_COLUMNS,
        &widths,
        &cursor,
        rows,
    );

    if view.show_frame {
        if let Some(summary) = &view.frame {
            let mut lines: Vec<Line> = summary.lines.iter().cloned().map(Line::from).collect();
            for e in &summary.errors {
                lines.push(Line::from(format!("error: {e}")).style(Style::new().fg(Color::Red)));
            }
            for w in &summary.warnings {
                lines.push(
                    Line::from(format!("warning: {w}")).style(Style::new().fg(Color::Yellow)),
                );
            }
            frame.render_widget(
                Paragraph::new(lines)
                    .block(Block::bordered().title(" fADC250 frame (f/Esc closes) ")),
                panel,
            );
            return;
        }
    }
    let (title, lines) = match app.selected_words() {
        Some((first, words)) => {
            let more = app.words_after_window();
            let title = format!(
                " words {}..{} of the selected structure{} ",
                first,
                first + words.len(),
                if more > 0 {
                    format!(", {more} more (n/p)")
                } else {
                    String::new()
                }
            );
            (title, hex_lines(first, &words))
        }
        None => (
            " words ".to_string(),
            vec!["no structure selected".to_string()],
        ),
    };
    let text: Vec<Line> = lines.into_iter().map(Line::from).collect();
    frame.render_widget(
        Paragraph::new(text).block(Block::bordered().title(title)),
        panel,
    );
}

fn draw_help(frame: &mut Frame, body: Rect) {
    let text: Vec<Line> = HELP_LINES.iter().map(|l| Line::from(*l)).collect();
    frame.render_widget(
        Paragraph::new(text)
            .block(Block::bordered().title(" help "))
            .wrap(Wrap { trim: false }),
        body,
    );
}

/// The text of a rendered buffer, one string per row (trailing spaces
/// kept), for tests with ratatui's `TestBackend`.
pub fn buffer_lines(buffer: &ratatui::buffer::Buffer) -> Vec<String> {
    let area = *buffer.area();
    (area.top()..area.bottom())
        .map(|y| {
            (area.left()..area.right())
                .filter_map(|x| buffer.cell((x, y)).map(|c| c.symbol().to_string()))
                .collect()
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    #[test]
    fn draws_an_empty_app_and_help() {
        let mut app = App::new("/nowhere/run.evio");
        let mut terminal = Terminal::new(TestBackend::new(80, 12)).unwrap();
        terminal.draw(|f| draw(f, &mut app)).unwrap();
        let text = buffer_lines(terminal.backend().buffer()).join("\n");
        assert!(text.contains(" 0 records "));
        assert!(text.contains("scanning:"));
        app.key(crate::state::Key::Char('?'));
        terminal.draw(|f| draw(f, &mut app)).unwrap();
        let text = buffer_lines(terminal.backend().buffer()).join("\n");
        assert!(text.contains("evio-tui keys"));
    }
}
