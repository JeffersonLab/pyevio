//! The golden-set ruler: every row of the evio-optim golden manifest.
//!
//! `tests/data/golden_manifest.tsv` is generated from
//! `evio-optim/space-evio-lib-23/golden_manifest/manifest.yaml` (path,
//! expected layout or `reject:<version>`, event count, sha256). The files
//! live under `EVIO_GOLDEN_ROOT` (default `/data/evio-lib/golden`); the test
//! skips with a note when that directory is absent.
//!
//! For a readable row the layout and the event count must match and no
//! check may fire. For a reject row (evio-library v4, v1—3) the reader must
//! end with `Error::UnsupportedVersion` naming the version, either at open
//! or, for a v4 file whose first block looks like an SRO block, on the block
//! that gives it away.

use std::path::{Path, PathBuf};

use evio_stream::{Bank, Error, FileLayout, RecordReader};

struct Row {
    path: String,
    expected: String,
    event_count: u64,
}

fn rows() -> Vec<Row> {
    let tsv = include_str!("data/golden_manifest.tsv");
    tsv.lines()
        .filter(|l| !l.starts_with('#') && !l.trim().is_empty())
        .map(|l| {
            let f: Vec<&str> = l.split('\t').collect();
            Row {
                path: f[0].to_string(),
                expected: f[1].to_string(),
                event_count: f[2].parse().expect("event count"),
            }
        })
        .collect()
}

fn golden_root() -> Option<PathBuf> {
    let root = std::env::var("EVIO_GOLDEN_ROOT").unwrap_or_else(|_| "/data/evio-lib/golden".into());
    let root = PathBuf::from(root);
    root.is_dir().then_some(root)
}

#[derive(Debug)]
struct Outcome {
    layout: FileLayout,
    events: u64,
    declared: u64,
    problems: Vec<String>,
}

/// Drain the reader; return the terminal error if any, and the counts.
fn read_all(path: &Path) -> Result<Outcome, Error> {
    let mut reader = RecordReader::open(path)?;
    let layout = reader.layout();
    let mut records = 0u64;
    let mut events = 0u64;
    let mut declared = 0u64;
    let mut problems = Vec::new();
    while let Some(record) = reader.next_record()? {
        if record.is_trailer() {
            assert_eq!(record.event_count(), 0, "a trailer yields no events");
            continue;
        }
        records += 1;
        events += record.event_count() as u64;
        declared += record.header.event_count as u64;
        problems.extend(record.errors.iter().cloned());
        for (i, event) in record.iter_events().enumerate() {
            // Every event is a bank whose length fits its span exactly.
            let bank = Bank::event(event)
                .unwrap_or_else(|e| panic!("record {} event {i}: {e}", record.index));
            assert_eq!(bank.total_words(), event.len(), "event span == bank length");
            // Walk the whole tree; a bad length is a failure for golden data.
            fn walk(bank: &Bank<'_>, errors: &mut Vec<String>) {
                for child in bank.children() {
                    match child {
                        Ok(c) => walk(&c, errors),
                        Err(e) => errors.push(e.to_string()),
                    }
                }
            }
            walk(&bank, &mut problems);
        }
        reader.recycle(record);
    }
    if let Some(t) = reader.truncated_tail() {
        problems.push(format!("truncated tail {t:?}"));
    }
    if let Some(b) = reader.bad_record_header() {
        problems.push(format!("bad record header {b:?}"));
    }
    let _ = records;
    Ok(Outcome {
        layout,
        events,
        declared,
        problems,
    })
}

#[test]
fn golden_manifest() {
    let Some(root) = golden_root() else {
        eprintln!("golden root absent, skipping (set EVIO_GOLDEN_ROOT)");
        return;
    };
    let rows = rows();
    assert!(rows.len() >= 40, "manifest rows");
    let mut passed = 0;
    let mut rejected = 0;
    for row in &rows {
        let path = root.join(&row.path);
        assert!(path.is_file(), "golden file missing: {}", path.display());
        let result = read_all(&path);
        if let Some(version) = row.expected.strip_prefix("reject:") {
            let version: u8 = version.parse().unwrap();
            match result {
                Err(Error::UnsupportedVersion { version: v, detail }) => {
                    assert_eq!(v, version, "{}: version in the error ({detail})", row.path);
                    let message = Error::UnsupportedVersion { version: v, detail }.to_string();
                    assert!(message.contains(&format!("version {version}")), "{message}");
                    assert!(message.contains("evio2xml"), "{message}");
                    rejected += 1;
                }
                other => panic!(
                    "{}: expected the version {version} error, got {other:?}",
                    row.path
                ),
            }
            continue;
        }
        let Outcome {
            layout,
            events,
            declared,
            problems,
        } = result.unwrap_or_else(|e| panic!("{}: {e}", row.path));
        let expected_layout = match row.expected.as_str() {
            "v6" => FileLayout::V6,
            "v6-records" => FileLayout::V6Records,
            other => panic!("unknown expectation {other}"),
        };
        assert_eq!(layout, expected_layout, "{}: layout", row.path);
        assert_eq!(events, row.event_count, "{}: events found", row.path);
        assert_eq!(
            declared, row.event_count,
            "{}: events declared by the headers",
            row.path
        );
        assert!(problems.is_empty(), "{}: {problems:?}", row.path);
        passed += 1;
    }
    eprintln!("golden: {passed} rows read, {rejected} rows rejected as expected");
    assert_eq!(passed + rejected, rows.len());
    assert_eq!(rejected, 7, "v4 (cpp and java, 3 each) and v1-3 rows");
}

#[test]
fn golden_from_a_pipe() {
    // The reader never seeks: the same counts must come from a non-seekable source.
    let Some(root) = golden_root() else {
        return;
    };
    let path = root.join("cases/cpp/lz4_le.evio");
    let bytes = std::fs::read(&path).unwrap();
    let mut reader = RecordReader::new(std::io::Cursor::new(bytes)).unwrap();
    let mut events = 0;
    while let Some(record) = reader.next_record().unwrap() {
        events += record.event_count();
    }
    assert_eq!(events, 18);
    assert!(reader.trailer_seen());
}

#[test]
fn truncated_and_garbage_tails() {
    let Some(root) = golden_root() else {
        return;
    };
    let path = root.join("cases/cpp/manyrec_le.evio");
    let bytes = std::fs::read(&path).unwrap();
    // Cut the file inside the body of record 7 (58 words, 2 events): the
    // complete events before the cut are kept. (A cut inside a record
    // header is a bad_record_header instead, as in pyevio.)
    let mut probe = RecordReader::new(std::io::Cursor::new(bytes.clone())).unwrap();
    let cut = loop {
        let record = probe.next_record().unwrap().expect("record 7");
        if record.index == 7 {
            break (record.offset + record.header.header_bytes() + 100) as usize;
        }
    };
    let mut reader = RecordReader::new(std::io::Cursor::new(bytes[..cut].to_vec())).unwrap();
    let mut events = 0;
    let mut last_truncated = false;
    while let Some(record) = reader.next_record().unwrap() {
        events += record.event_count();
        last_truncated = record.is_truncated;
    }
    let tail = reader.truncated_tail().expect("truncated tail");
    assert!(last_truncated);
    assert!(tail.present_bytes < tail.declared_bytes);
    assert!(events < 18 && events > 0, "{events}");
    // Garbage after the last record: bad_record_header, no panic.
    let mut garbage = bytes.clone();
    garbage.extend_from_slice(&[0xAB; 40]);
    let mut reader = RecordReader::new(std::io::Cursor::new(garbage)).unwrap();
    let mut events = 0;
    while let Some(record) = reader.next_record().unwrap() {
        events += record.event_count();
    }
    // manyrec ends in a trailer, so the garbage after it is never read.
    assert_eq!(events, 18);
    // A record with the last-record bit followed by garbage ends quietly, as
    // in pyevio (the .dump's one record carries the bit).
    let path = root.join("cases/cpp/buffer_evw_le.dump");
    let dump = std::fs::read(&path).unwrap();
    let mut garbage = dump.clone();
    garbage.extend_from_slice(&[0xAB; 40]);
    let mut reader = RecordReader::new(std::io::Cursor::new(garbage)).unwrap();
    let mut events = 0;
    let mut last_bit = false;
    while let Some(record) = reader.next_record().unwrap() {
        events += record.event_count();
        last_bit = record.header.is_last_record;
    }
    assert_eq!(events, 18);
    assert!(last_bit);
    assert!(reader.bad_record_header().is_none());
    // The same events in an SRO-style 8-word block (no last bit, no index
    // array) followed by garbage: bad_record_header names the offset.
    let mut probe = RecordReader::new(std::io::Cursor::new(dump)).unwrap();
    let record = probe.next_record().unwrap().unwrap();
    let data = record.data().to_vec();
    let n_events = record.header.event_count;
    let mut sro = Vec::new();
    let length_words = 8 + data.len() as u32 / 4;
    for word in [length_words, 1, 8, n_events, 0, 4, 0, 0xc0da_0100] {
        sro.extend_from_slice(&word.to_le_bytes());
    }
    sro.extend_from_slice(&data);
    let block_bytes = sro.len() as u64;
    sro.extend_from_slice(&[0xAB; 40]);
    let mut reader = RecordReader::new(std::io::Cursor::new(sro)).unwrap();
    assert_eq!(reader.layout(), FileLayout::Sro8Word);
    let mut events = 0;
    while let Some(record) = reader.next_record().unwrap() {
        events += record.event_count();
    }
    assert_eq!(events, 18);
    let bad = reader.bad_record_header().expect("garbage after the block");
    assert_eq!(bad.offset, block_bytes);
    assert!(bad.message.contains("no magic number"), "{}", bad.message);
}
