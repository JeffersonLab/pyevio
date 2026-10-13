//! `evio-dump FILE|- [--record N] [--event M] [--words K]`: the file header
//! and every record header; with `--record N` that record's header in full
//! and its events; with `--event M` the bank tree of event M of record N
//! (tag, type, num, length) with the first K words of each leaf in hex.

use std::process::ExitCode;

use evio_stream::{Bank, Error, Record, RecordReader, StructureKind, Words};

const USAGE: &str = "usage: evio-dump FILE|- [--record N] [--event M] [--words K]\n\
  Without options: the file header and one line per record header.\n\
  --record N: the full header of record N and its event table.\n\
  --event M:  the bank tree of event M of record N (default record 0), with the\n\
              first K words (default 8) of each leaf in hex.";

struct Options {
    path: String,
    record: Option<usize>,
    event: Option<usize>,
    words: usize,
}

fn parse_args() -> Result<Options, String> {
    let mut path = None;
    let mut record = None;
    let mut event = None;
    let mut words = 8usize;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let mut value = |name: &str| -> Result<usize, String> {
            let v = args.next().ok_or_else(|| format!("{name} needs a value"))?;
            v.parse().map_err(|_| format!("{name}: not a number: {v}"))
        };
        match arg.as_str() {
            "--record" => record = Some(value("--record")?),
            "--event" => event = Some(value("--event")?),
            "--words" => words = value("--words")?,
            "-h" | "--help" => return Err(String::new()),
            "-" => path = Some(arg),
            a if a.starts_with('-') => return Err(format!("unknown option {a}")),
            _ => {
                if path.is_some() {
                    return Err("one file only".into());
                }
                path = Some(arg);
            }
        }
    }
    let path = path.ok_or_else(|| "no file given".to_string())?;
    Ok(Options {
        path,
        record,
        event,
        words,
    })
}

fn print_file_header<R: std::io::Read>(reader: &RecordReader<R>) {
    let h = reader.file_header();
    println!("layout:             {}", reader.layout());
    println!("byte order:         {}", reader.order());
    println!("file type:          {}", h.file_type);
    if reader.layout() == evio_stream::FileLayout::V6 {
        println!("file number:        {}", h.file_number);
        println!("header length:      {} words", h.header_length);
        println!("record count:       {} (declared)", h.record_count);
        println!("index array length: {} bytes", h.index_array_length);
        println!("version:            {}", h.version);
        println!("user header length: {} bytes", h.user_header_length);
        println!(
            "dictionary / first event / trailer index: {} / {} / {}",
            h.has_dictionary, h.has_first_event, h.has_trailer
        );
        println!("user register:      0x{:016x}", h.user_register);
        println!("trailer position:   0x{:x}", h.trailer_position);
        println!(
            "user integers:      {} {}",
            h.user_integer1, h.user_integer2
        );
    } else {
        println!(
            "version:            {} (container headers say {})",
            h.version, h.container_version
        );
    }
    println!();
}

fn record_line(record: &Record) {
    let h = &record.header;
    let mut flags = Vec::new();
    if h.is_last_record {
        flags.push("last");
    }
    if h.is_trailer() {
        flags.push("trailer");
    }
    if record.is_truncated {
        flags.push("truncated");
    }
    if !record.errors.is_empty() {
        flags.push("errors");
    }
    println!(
        "{:>6} 0x{:<12X} {:>9} {:>3} {:>6} {:>6} {:>8} {:>8} {:<20} {}",
        record.index,
        record.offset,
        h.record_length,
        h.header_length,
        h.event_count,
        record.event_count(),
        h.compression.name(),
        h.record_number,
        h.event_type_name(),
        flags.join(",")
    );
}

fn print_record(record: &Record) {
    let h = &record.header;
    println!("record {} at offset 0x{:X}", record.index, record.offset);
    println!(
        "  record length:        {} words ({} bytes)",
        h.record_length,
        h.record_bytes()
    );
    println!("  record number:        {}", h.record_number);
    println!("  header length:        {} words", h.header_length);
    println!(
        "  event count:          {} declared, {} found",
        h.event_count,
        record.event_count()
    );
    println!("  index array length:   {} bytes", h.index_array_length);
    println!("  version:              {}", h.version);
    println!(
        "  header type:          {} ({})",
        h.header_type,
        if h.is_trailer() { "trailer" } else { "record" }
    );
    println!(
        "  bit info:             0x{:05X}  last {}  dictionary {}  first event {}  event type {}",
        h.bit_info,
        h.is_last_record,
        h.has_dictionary,
        h.has_first_event,
        h.event_type_name()
    );
    println!("  pad1/pad2/pad3:       {} {} {}", h.pad1, h.pad2, h.pad3);
    println!("  user header length:   {} bytes", h.user_header_length);
    println!(
        "  uncompressed length:  {} bytes",
        h.uncompressed_data_length
    );
    println!(
        "  compression:          {} ({} words)",
        h.compression.name(),
        h.compressed_data_length
    );
    println!(
        "  user registers:       0x{:016x} 0x{:016x}",
        h.user_register1, h.user_register2
    );
    println!(
        "  truncated:            {} (declared {} bytes, present {})",
        record.is_truncated, record.declared_bytes, record.present_bytes
    );
    for e in &record.errors {
        println!("  error: {e}");
    }
    println!("  events (offset into the record buffer, bytes):");
    for (i, span) in record.events().iter().enumerate() {
        let tag = record
            .event(i)
            .and_then(|w| w.get(1))
            .map(|w| {
                format!(
                    "tag 0x{:04X} type 0x{:02X} num {}",
                    w >> 16,
                    (w >> 8) & 0x3F,
                    w & 0xFF
                )
            })
            .unwrap_or_default();
        println!(
            "  {i:>6}  0x{:<10X} {:>10}  {tag}",
            span.offset, span.length
        );
    }
}

fn hex_words(words: Words<'_>, limit: usize) -> String {
    let mut out: Vec<String> = words
        .iter()
        .take(limit)
        .map(|w| format!("{w:08x}"))
        .collect();
    if words.len() > limit {
        out.push(format!("... ({} words)", words.len()));
    }
    out.join(" ")
}

fn print_tree(bank: &Bank<'_>, depth: usize, words: usize) {
    let indent = "  ".repeat(depth);
    let num = match bank.kind() {
        StructureKind::Bank => format!(" num {}", bank.num),
        _ => String::new(),
    };
    println!(
        "{indent}{} tag 0x{:04X} type 0x{:02X} ({}){num} length {} ({} words)",
        bank.kind().name(),
        bank.tag,
        bank.data_type,
        bank.type_name(),
        bank.length,
        bank.total_words()
    );
    if bank.is_container() {
        for child in bank.children() {
            match child {
                Ok(child) => print_tree(&child, depth + 1, words),
                Err(e) => println!("{indent}  ERROR {e}"),
            }
        }
    } else if words > 0 {
        println!("{indent}  {}", hex_words(bank.data(), words));
    }
}

fn run(options: &Options) -> Result<(), Error> {
    let mut reader = if options.path == "-" {
        RecordReaderKind::Stdin(RecordReader::stdin()?)
    } else {
        RecordReaderKind::File(RecordReader::open(&options.path)?)
    };
    match &reader {
        RecordReaderKind::File(r) => print_file_header(r),
        RecordReaderKind::Stdin(r) => print_file_header(r),
    }
    let want_record = options.record.or(if options.event.is_some() {
        Some(0)
    } else {
        None
    });
    if want_record.is_none() {
        println!(
            " index offset            length hdr events  found compress   number type                 flags"
        );
    }
    loop {
        let next = match &mut reader {
            RecordReaderKind::File(r) => r.next_record()?,
            RecordReaderKind::Stdin(r) => r.next_record()?,
        };
        let Some(record) = next else { break };
        match want_record {
            None => record_line(&record),
            Some(n) if n == record.index => {
                print_record(&record);
                if let Some(m) = options.event {
                    println!();
                    match record.event(m) {
                        Some(event) => {
                            println!("event {m} of record {n}: {} words", event.len());
                            match Bank::event(event) {
                                Ok(bank) => print_tree(&bank, 0, options.words),
                                Err(e) => println!("ERROR {e}"),
                            }
                        }
                        None => println!(
                            "record {n} has no event {m} ({} events)",
                            record.event_count()
                        ),
                    }
                }
                return Ok(());
            }
            Some(_) => {}
        }
    }
    let (tail, bad, records) = match &reader {
        RecordReaderKind::File(r) => (
            r.truncated_tail(),
            r.bad_record_header().cloned(),
            r.data_records(),
        ),
        RecordReaderKind::Stdin(r) => (
            r.truncated_tail(),
            r.bad_record_header().cloned(),
            r.data_records(),
        ),
    };
    if let Some(n) = want_record {
        println!("no record {n}: the stream holds {records} data records");
    }
    if let Some(t) = tail {
        println!(
            "truncated_tail: declared {} bytes, present {}, salvaged {} events",
            t.declared_bytes, t.present_bytes, t.salvaged_events
        );
    }
    if let Some(b) = bad {
        println!("bad_record_header at 0x{:X}: {}", b.offset, b.message);
    }
    Ok(())
}

enum RecordReaderKind {
    File(RecordReader<std::fs::File>),
    Stdin(RecordReader<std::io::StdinLock<'static>>),
}

fn main() -> ExitCode {
    let options = match parse_args() {
        Ok(o) => o,
        Err(message) if message.is_empty() => {
            println!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        Err(message) => {
            eprintln!("{message}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    match run(&options) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("evio-dump: {e}");
            ExitCode::FAILURE
        }
    }
}
