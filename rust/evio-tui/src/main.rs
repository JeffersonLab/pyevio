//! `evio-tui FILE`: a minimal terminal viewer for EVIO v6 files and SRO
//! streams. The file is scanned on a background thread while the records
//! list fills; Enter opens a record, then an event; `?` lists the keys.

use std::io;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::mpsc::Receiver;
use std::time::Duration;

use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::DefaultTerminal;

use evio_tui::scan::{self, ScanMessage};
use evio_tui::state::{App, Key};
use evio_tui::ui;

const USAGE: &str = "usage: evio-tui FILE\n\
  Shows the records of an EVIO v6 file or SRO stream, the events of a record and the\n\
  bank tree and words of an event. Keys: ? for help, q to quit.\n\
  Standard input is not supported: the viewer opens records by seeking to their offset.";

/// How long one loop iteration waits for a key before redrawing.
const TICK: Duration = Duration::from_millis(50);

fn main() -> ExitCode {
    let mut path = None;
    for arg in std::env::args_os().skip(1) {
        let text = arg.to_string_lossy().into_owned();
        match text.as_str() {
            "-h" | "--help" => {
                println!("{USAGE}");
                return ExitCode::SUCCESS;
            }
            "-" => {
                eprintln!("evio-tui: standard input is not supported; random access to records needs a file.\n{USAGE}");
                return ExitCode::from(2);
            }
            a if a.starts_with('-') => {
                eprintln!("unknown option {a}\n{USAGE}");
                return ExitCode::from(2);
            }
            _ => {
                if path.is_some() {
                    eprintln!("one file only\n{USAGE}");
                    return ExitCode::from(2);
                }
                path = Some(PathBuf::from(arg));
            }
        }
    }
    let Some(path) = path else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    // Open once before taking the terminal so a missing or unsupported
    // file is reported on stderr, not as an empty screen.
    if let Err(e) = evio_stream::RecordReader::open(&path) {
        eprintln!("evio-tui: {}: {e}", path.display());
        return ExitCode::FAILURE;
    }

    let rx = scan::spawn(path.clone());
    let mut app = App::new(path);
    let mut terminal = ratatui::init();
    let result = run(&mut terminal, &mut app, &rx);
    ratatui::restore();
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("evio-tui: {e}");
            ExitCode::FAILURE
        }
    }
}

/// The loop: drain the scan channel, draw, wait up to one tick for a key.
fn run(
    terminal: &mut DefaultTerminal,
    app: &mut App,
    rx: &Receiver<ScanMessage>,
) -> io::Result<()> {
    loop {
        // Empty and Disconnected both end the drain; a finished scan
        // thread has already sent its last message.
        while let Ok(message) = rx.try_recv() {
            app.apply(message);
        }
        terminal.draw(|frame| ui::draw(frame, app))?;
        if event::poll(TICK)? {
            if let Event::Key(key) = event::read()? {
                if key.kind == KeyEventKind::Press {
                    if let Some(key) = map_key(key) {
                        app.key(key);
                    }
                }
            }
        }
        if app.quit {
            return Ok(());
        }
    }
}

/// Translate a crossterm key press; `None` for keys the viewer ignores.
fn map_key(key: KeyEvent) -> Option<Key> {
    Some(match key.code {
        KeyCode::Up => Key::Up,
        KeyCode::Down => Key::Down,
        KeyCode::PageUp => Key::PageUp,
        KeyCode::PageDown => Key::PageDown,
        KeyCode::Home => Key::Home,
        KeyCode::End => Key::End,
        KeyCode::Enter => Key::Enter,
        KeyCode::Esc => Key::Esc,
        KeyCode::Backspace | KeyCode::Left => Key::Esc,
        KeyCode::Right => Key::Enter,
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => Key::Char('q'),
        KeyCode::Char(c) => Key::Char(c),
        _ => return None,
    })
}
