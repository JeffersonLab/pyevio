//! The CODA streaming fADC250 frame walk.
//!
//! A streaming time frame is one EVIO event (tag 0xFF60, a bank of banks):
//!
//! ```text
//! event bank          tag 0xFF60, type 0x10, num 0
//!   SIB               tag 0xFF31, type 0x20 (bank of segments)
//!     TSS segment     tag 0x32, length 3: {frame number, timestamp low, timestamp high}
//!     AIS segment     tag 0x42: aggregated-stream entries (counted, never walked)
//!   ROC bank          tag = ROC id, type 0x10, num = stream status byte
//!     stream-info     tag 0xFF30, type 0x20: TSS 0x31 + payload-port map 0x41
//!     payload bank    tag = payload port, type 0x00, one word per hit
//!     payload bank    tag = payload port, length 1, num 1: an empty port
//!   ROC bank          ...
//! ```
//!
//! The walk mirrors `pyevio.decoders.fadc250_stream`: every bank end is
//! checked against its container and a bad length ends the walk of that
//! event with a counted error. The module behind each payload port comes
//! from the 0x41 map of the ROC bank's stream-info bank; only fADC250 ports
//! (and unmapped ones) yield hits, the words of other modules (DCRB) are
//! counted. The AIS segment is counted (entries, distinct values) and never
//! used to locate ROC banks.
//!
//! Hit word: bit 31 flag (never set in the SRO data), bits 30—17 time in
//! 4 ns ticks, bits 16—13 channel, bits 12—0 charge (8191 = overflow).

use crate::words::Words;

pub const STREAM_EVENT_TAG: u16 = 0xFF60;
pub const SIB_TAG: u16 = 0xFF31;
pub const SIB_TSS_SEGMENT_TAG: u8 = 0x32;
pub const SIB_AIS_SEGMENT_TAG: u8 = 0x42;
pub const ROC_STREAM_INFO_TAG: u16 = 0xFF30;
pub const ROC_TSS_SEGMENT_TAG: u8 = 0x31;
pub const ROC_PAYLOAD_PORT_SEGMENT_TAG: u8 = 0x41;

/// Module id of an fADC250 in the payload-port map.
pub const MODULE_FADC250: u8 = 0;
/// Module id of a DCRB in the payload-port map.
pub const MODULE_DCRB: u8 = 1;
/// Stand-in module id of a port absent from the map (decoded as fADC250).
pub const MODULE_UNKNOWN: u8 = 0xFF;

/// Length of one hit-time tick in nanoseconds (the fADC250 sample period).
pub const HIT_TIME_TICK_NS: u16 = 4;

/// Error and warning messages kept per frame; the counts are always exact.
pub const MAX_MESSAGES: usize = 50;

/// Name of a module id: "fADC250", "DCRB", "unmapped" or "module N".
pub fn module_name(module: u8) -> String {
    match module {
        MODULE_FADC250 => "fADC250".into(),
        MODULE_DCRB => "DCRB".into(),
        MODULE_UNKNOWN => "unmapped".into(),
        other => format!("module {other}"),
    }
}

/// One fADC250 hit.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Hit {
    pub frame: u32,
    pub timestamp: u64,
    pub roc: u16,
    /// Stream mask from the ROC bank num byte (1 or 2).
    pub stream: u8,
    pub port: u16,
    pub channel: u8,
    /// Time from the frame start in 4 ns ticks (14 bits).
    pub time_raw: u16,
    /// 13-bit charge; 8191 means overflow.
    pub charge: u16,
    /// Bit 31 of the word; never set in the SRO data.
    pub flag: bool,
}

impl Hit {
    /// Split a hit word into `(channel, time_raw, charge, flag)`.
    #[inline(always)]
    pub fn unpack(word: u32) -> (u8, u16, u16, bool) {
        (
            ((word >> 13) & 0xF) as u8,
            ((word >> 17) & 0x3FFF) as u16,
            (word & 0x1FFF) as u16,
            word >> 31 != 0,
        )
    }

    /// Time from the frame start in nanoseconds.
    pub fn t_ns(&self) -> u16 {
        self.time_raw * HIT_TIME_TICK_NS
    }
}

/// What one ROC bank of a frame held.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RocBank {
    pub roc: u16,
    /// Stream mask (bits 3—0 of the num byte).
    pub stream: u8,
    /// The whole num byte.
    pub stream_status: u8,
    /// Module of the first payload bank; [`MODULE_UNKNOWN`] without any.
    pub module: u8,
    pub n_payload_banks: u32,
    pub n_empty_payload_banks: u32,
    pub n_unmapped_payload_banks: u32,
    pub n_hits: u64,
    pub non_fadc_words: u64,
}

/// A payload bank located by the walk: word offsets into the event.
#[derive(Clone, Copy, Debug)]
struct PayloadRef {
    /// First data word (after the 2-word bank header).
    start: u32,
    n_words: u32,
    roc: u16,
    port: u16,
    stream: u8,
    module: u8,
}

/// One payload bank of a frame.
#[derive(Clone, Copy, Debug)]
pub struct PayloadBank<'a> {
    pub roc: u16,
    pub stream: u8,
    pub port: u16,
    pub module: u8,
    /// The data words (hits for an fADC250 port).
    pub words: Words<'a>,
}

impl PayloadBank<'_> {
    /// True for a length-1 bank (no hit on the port in this frame).
    pub fn is_empty(&self) -> bool {
        self.words.is_empty()
    }

    /// True when the words are fADC250 hits (module fADC250 or unmapped).
    pub fn is_fadc250(&self) -> bool {
        self.module == MODULE_FADC250 || self.module == MODULE_UNKNOWN
    }
}

/// Walks stream frames. Holds reusable scratch space so decoding a frame
/// allocates nothing in the common case.
#[derive(Default)]
pub struct FrameDecoder {
    roc_banks: Vec<RocBank>,
    payload: Vec<PayloadRef>,
    ais: Vec<u32>,
    errors: Vec<String>,
    warnings: Vec<String>,
}

/// One decoded frame, borrowing the event and the decoder.
pub struct Frame<'a> {
    event: Words<'a>,
    pub frame_number: u32,
    pub timestamp: u64,
    pub roc_banks: &'a [RocBank],
    payload: &'a [PayloadRef],
    pub n_payload_banks: u32,
    pub n_empty_payload_banks: u32,
    pub n_unmapped_payload_banks: u32,
    /// Words of every payload bank (hits plus other-module words).
    pub n_payload_words: u64,
    /// fADC250 hit words (= sum of payload lengths of fADC250 ports).
    pub n_hits: u64,
    /// Payload words of other modules (DCRB).
    pub non_fadc_words: u64,
    /// Entries of the AIS segment, 0 without one.
    pub n_ais_entries: u32,
    /// Distinct values among the AIS entries.
    pub n_ais_distinct: u32,
    /// Bad lengths met; the walk of the structure stopped there.
    pub n_errors: u32,
    /// Malformed SIB segments after the TSS; no hit depends on them.
    pub n_warnings: u32,
    /// The first [`MAX_MESSAGES`] error messages.
    pub errors: &'a [String],
    /// The first [`MAX_MESSAGES`] warning messages.
    pub warnings: &'a [String],
}

impl<'a> Frame<'a> {
    /// Every payload bank in walk order.
    pub fn payload_banks(&self) -> impl Iterator<Item = PayloadBank<'a>> + '_ {
        let event = self.event;
        self.payload.iter().map(move |p| PayloadBank {
            roc: p.roc,
            stream: p.stream,
            port: p.port,
            module: p.module,
            words: event
                .slice(p.start as usize, p.start as usize + p.n_words as usize)
                .unwrap_or(Words::new(&[], event.order())),
        })
    }

    /// Every fADC250 hit of the frame, in walk order.
    pub fn hits(&self) -> Hits<'a> {
        Hits {
            event: self.event,
            frame: self.frame_number,
            timestamp: self.timestamp,
            payload: self.payload,
            next_bank: 0,
            current: None,
            pos: 0,
        }
    }
}

/// Iterator over the hits of a frame (see [`Frame::hits`]).
pub struct Hits<'a> {
    event: Words<'a>,
    frame: u32,
    timestamp: u64,
    payload: &'a [PayloadRef],
    next_bank: usize,
    current: Option<(PayloadRef, Words<'a>)>,
    pos: usize,
}

impl Iterator for Hits<'_> {
    type Item = Hit;

    #[inline]
    fn next(&mut self) -> Option<Hit> {
        loop {
            if let Some((p, words)) = &self.current {
                if let Some(word) = words.get(self.pos) {
                    self.pos += 1;
                    let (channel, time_raw, charge, flag) = Hit::unpack(word);
                    return Some(Hit {
                        frame: self.frame,
                        timestamp: self.timestamp,
                        roc: p.roc,
                        stream: p.stream,
                        port: p.port,
                        channel,
                        time_raw,
                        charge,
                        flag,
                    });
                }
                self.current = None;
            }
            let p = *self.payload.get(self.next_bank)?;
            self.next_bank += 1;
            if p.module != MODULE_FADC250 && p.module != MODULE_UNKNOWN {
                continue;
            }
            let start = p.start as usize;
            let words = self
                .event
                .slice(start, start + p.n_words as usize)
                .unwrap_or(Words::new(&[], self.event.order()));
            self.current = Some((p, words));
            self.pos = 0;
        }
    }
}

impl FrameDecoder {
    /// A decoder with empty scratch space.
    pub fn new() -> Self {
        Self::default()
    }

    fn error(&mut self, n_errors: &mut u32, word: usize, reason: String) {
        *n_errors += 1;
        if self.errors.len() < MAX_MESSAGES {
            self.errors.push(format!("word {word}: {reason}"));
        }
    }

    fn warning(&mut self, n_warnings: &mut u32, word: usize, reason: String) {
        *n_warnings += 1;
        if self.warnings.len() < MAX_MESSAGES {
            self.warnings.push(format!("word {word}: {reason}"));
        }
    }

    /// Decode one event. `None` when the event is no stream frame (its tag
    /// is not 0xFF60); malformed lengths yield what was walked before them
    /// plus counted errors, never a panic.
    pub fn decode<'a>(&'a mut self, event: Words<'a>) -> Option<Frame<'a>> {
        let n = event.len();
        let w = |i: usize| event.get(i).unwrap_or(0);
        if n < 2 || (w(1) >> 16) as u16 != STREAM_EVENT_TAG {
            return None;
        }
        self.roc_banks.clear();
        self.payload.clear();
        self.errors.clear();
        self.warnings.clear();
        let mut n_errors = 0u32;
        let mut n_warnings = 0u32;

        let mut event_end = w(0) as usize + 1;
        if event_end > n {
            self.error(
                &mut n_errors,
                0,
                format!("event length {event_end} words runs past the {n} words given"),
            );
            event_end = n;
        }

        let sib = self.read_sib(event, event_end, &mut n_errors, &mut n_warnings);
        let mut totals = Totals::default();

        let mut roc_i = sib.end;
        while roc_i < event_end {
            if roc_i + 1 >= event_end {
                self.error(
                    &mut n_errors,
                    roc_i,
                    "ROC bank header runs past the event end".into(),
                );
                break;
            }
            let roc_len = w(roc_i) as usize;
            let roc_end = roc_i + roc_len + 1;
            if roc_end > event_end {
                self.error(
                    &mut n_errors,
                    roc_i,
                    format!("ROC bank length {roc_len} runs past the event end {event_end}"),
                );
                break;
            }
            let roc_header = w(roc_i + 1);
            let dt = ((roc_header >> 8) & 0x3F) as u8;
            if dt != 0x10 && dt != 0x0E {
                self.error(
                    &mut n_errors,
                    roc_i,
                    format!("ROC bank 0x{roc_header:08X} is not a bank of banks"),
                );
                roc_i = roc_end;
                continue;
            }
            self.walk_roc_bank(
                event,
                roc_i + 2,
                roc_end,
                roc_header,
                &mut totals,
                &mut n_errors,
            );
            roc_i = roc_end;
        }

        Some(Frame {
            event,
            frame_number: sib.frame_number,
            timestamp: sib.timestamp,
            roc_banks: &self.roc_banks,
            payload: &self.payload,
            n_payload_banks: totals.payload_banks,
            n_empty_payload_banks: totals.empty_banks,
            n_unmapped_payload_banks: totals.unmapped_banks,
            n_payload_words: totals.payload_words,
            n_hits: totals.hits,
            non_fadc_words: totals.non_fadc_words,
            n_ais_entries: sib.n_ais_entries,
            n_ais_distinct: sib.n_ais_distinct,
            n_errors,
            n_warnings,
            errors: &self.errors,
            warnings: &self.warnings,
        })
    }

    /// The SIB at word 2: frame number, timestamp, where the ROC banks
    /// start (from the SIB length word alone), and the AIS counts.
    fn read_sib(
        &mut self,
        event: Words<'_>,
        event_end: usize,
        n_errors: &mut u32,
        n_warnings: &mut u32,
    ) -> Sib {
        let w = |i: usize| event.get(i).unwrap_or(0);
        let mut sib = Sib {
            end: event_end,
            ..Sib::default()
        };
        if 3 >= event_end {
            self.error(n_errors, 2, "no room for the stream info bank".into());
            return sib;
        }
        let sib_len = w(2) as usize;
        let sib_end = 2 + sib_len + 1;
        if sib_end > event_end {
            self.error(
                n_errors,
                2,
                format!("SIB length {sib_len} runs past the event end {event_end}"),
            );
            return sib;
        }
        sib.end = sib_end;
        let tag = (w(3) >> 16) as u16;
        if tag != SIB_TAG {
            self.error(
                n_errors,
                3,
                format!("expected SIB tag 0x{SIB_TAG:04X}, found 0x{tag:04X}"),
            );
            return sib;
        }
        // The TSS is the first segment: tag<<24 | pad<<22 | type<<16 | length.
        let tss = w(4);
        if 7 >= sib_end || (tss >> 24) as u8 != SIB_TSS_SEGMENT_TAG || (tss & 0xFFFF) < 3 {
            self.error(
                n_errors,
                4,
                "SIB does not start with a time slice segment (tag 0x32, length 3)".into(),
            );
            return sib;
        }
        sib.frame_number = w(5);
        sib.timestamp = w(6) as u64 | ((w(7) as u64) << 32);
        let next = 4 + (tss & 0xFFFF) as usize + 1;
        self.count_ais(event, next, sib_end, &mut sib, n_warnings);
        sib
    }

    /// Entries and distinct values of the 0x42 segment among the SIB
    /// segments from `segment_i`. A missing segment, an empty one or one
    /// whose length runs past the SIB end leaves the counts at 0; the last
    /// two are warnings, not errors.
    fn count_ais(
        &mut self,
        event: Words<'_>,
        mut segment_i: usize,
        sib_end: usize,
        sib: &mut Sib,
        n_warnings: &mut u32,
    ) {
        let w = |i: usize| event.get(i).unwrap_or(0);
        while segment_i < sib_end {
            let header = w(segment_i);
            let segment_len = (header & 0xFFFF) as usize;
            let segment_end = segment_i + segment_len + 1;
            if segment_end > sib_end {
                self.warning(
                    n_warnings,
                    segment_i,
                    format!("SIB segment length {segment_len} runs past the SIB end {sib_end}"),
                );
                return;
            }
            if (header >> 24) as u8 == SIB_AIS_SEGMENT_TAG {
                if segment_len == 0 {
                    self.warning(n_warnings, segment_i, "AIS segment holds no entries".into());
                    return;
                }
                self.ais.clear();
                self.ais.extend((segment_i + 1..segment_end).map(w));
                self.ais.sort_unstable();
                self.ais.dedup();
                sib.n_ais_entries = segment_len as u32;
                sib.n_ais_distinct = self.ais.len() as u32;
                return;
            }
            segment_i = segment_end;
        }
    }

    /// `port -> module` from the 0x41 segment of the stream-info bank at
    /// `info_i`. Entries are uint16, two per word in memory order; pad 2
    /// means an odd entry count. Ports absent from the map stay
    /// [`MODULE_UNKNOWN`].
    fn read_payload_port_map(
        &mut self,
        event: Words<'_>,
        info_i: usize,
        info_end: usize,
        n_errors: &mut u32,
    ) -> [u8; 32] {
        let w = |i: usize| event.get(i).unwrap_or(0);
        let mut map = [MODULE_UNKNOWN; 32];
        let mut segment_i = info_i + 2;
        while segment_i < info_end {
            let header = w(segment_i);
            let segment_len = (header & 0xFFFF) as usize;
            let segment_end = segment_i + segment_len + 1;
            if segment_end > info_end {
                self.error(
                    n_errors,
                    segment_i,
                    format!("stream-info segment length {segment_len} runs past the bank end {info_end}"),
                );
                break;
            }
            if (header >> 24) as u8 == ROC_PAYLOAD_PORT_SEGMENT_TAG {
                let pad = (header >> 22) & 3;
                let n_entries = 2 * segment_len - usize::from(pad == 2);
                let first_half = (segment_i + 1) * 2;
                for entry_i in 0..n_entries {
                    let entry = event.get_u16(first_half + entry_i).unwrap_or(0);
                    let port = (entry & 0x1F) as usize;
                    if port != 0 {
                        map[port] = ((entry >> 8) & 0xF) as u8;
                    }
                }
                break;
            }
            segment_i = segment_end;
        }
        map
    }

    fn walk_roc_bank(
        &mut self,
        event: Words<'_>,
        mut child_i: usize,
        roc_end: usize,
        roc_header: u32,
        totals: &mut Totals,
        n_errors: &mut u32,
    ) {
        let w = |i: usize| event.get(i).unwrap_or(0);
        let roc = (roc_header >> 16) as u16;
        let stream_status = (roc_header & 0xFF) as u8;
        let stream = stream_status & 0xF;
        let mut bank = RocBank {
            roc,
            stream,
            stream_status,
            module: MODULE_UNKNOWN,
            ..RocBank::default()
        };
        let mut map = [MODULE_UNKNOWN; 32];
        // The stream-info bank comes first; its 0x41 segment names the module behind each port.
        if child_i + 1 < roc_end && (w(child_i + 1) >> 16) as u16 == ROC_STREAM_INFO_TAG {
            let info_end = (child_i + w(child_i) as usize + 1).min(roc_end);
            map = self.read_payload_port_map(event, child_i, info_end, n_errors);
            child_i = info_end;
        }
        while child_i < roc_end {
            if child_i + 1 >= roc_end {
                self.error(
                    n_errors,
                    child_i,
                    "payload bank header runs past the ROC bank end".into(),
                );
                break;
            }
            let payload_len = w(child_i) as usize;
            if payload_len == 0 || child_i + payload_len + 1 > roc_end {
                self.error(
                    n_errors,
                    child_i,
                    format!(
                        "payload bank length {payload_len} runs past the ROC bank end {roc_end}"
                    ),
                );
                break;
            }
            let port = (w(child_i + 1) >> 16) as u16;
            let module = map.get(port as usize).copied().unwrap_or(MODULE_UNKNOWN);
            if module == MODULE_UNKNOWN {
                bank.n_unmapped_payload_banks += 1;
            }
            if bank.module == MODULE_UNKNOWN {
                bank.module = module;
            }
            let n_words = (payload_len - 1) as u64;
            bank.n_payload_banks += 1;
            totals.payload_words += n_words;
            if n_words == 0 {
                bank.n_empty_payload_banks += 1;
            } else if module == MODULE_FADC250 || module == MODULE_UNKNOWN {
                bank.n_hits += n_words;
            } else {
                bank.non_fadc_words += n_words;
            }
            self.payload.push(PayloadRef {
                start: (child_i + 2) as u32,
                n_words: n_words as u32,
                roc,
                port,
                stream,
                module,
            });
            child_i += payload_len + 1;
        }
        totals.payload_banks += bank.n_payload_banks;
        totals.empty_banks += bank.n_empty_payload_banks;
        totals.unmapped_banks += bank.n_unmapped_payload_banks;
        totals.hits += bank.n_hits;
        totals.non_fadc_words += bank.non_fadc_words;
        self.roc_banks.push(bank);
    }
}

#[derive(Default)]
struct Sib {
    frame_number: u32,
    timestamp: u64,
    end: usize,
    n_ais_entries: u32,
    n_ais_distinct: u32,
}

#[derive(Default)]
struct Totals {
    payload_banks: u32,
    empty_banks: u32,
    unmapped_banks: u32,
    payload_words: u64,
    hits: u64,
    non_fadc_words: u64,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::words::ByteOrder;

    /// A frame with one ROC bank (ROC 7, stream 1) mapping port 1 to fADC250
    /// and port 3 to DCRB; port 1 holds two hits, port 3 two words, port 5
    /// is empty and unmapped.
    fn frame_words() -> Vec<u32> {
        let mut v = vec![
            0, // event length (patched)
            0xFF60_1000,
            0, // SIB length (patched)
            0xFF31_2000,
            0x3201_0003, // TSS
            42,          // frame number
            0x1000,      // timestamp low
            0x2,         // timestamp high
            0x4201_0003, // AIS, 3 entries
            7,
            7,
            7,
        ];
        v[2] = (v.len() - 3) as u32;
        let roc_start = v.len();
        v.extend([
            0,           // ROC bank length (patched)
            0x0007_1021, // ROC 7, bank of banks, num 0x21
            0,           // stream-info length (patched)
            0xFF30_2000,
            0x3101_0003, // TSS
            42,
            0x1000,
            0x2,
            0x4105_0001, // payload map: tag 0x41, pad 0, 1 word = two entries
            0x0103_0001, // LE: low half 0x0001 (port 1, module 0), high half 0x0103 (port 3, module 1)
        ]);
        let info_i = roc_start + 2;
        v[info_i] = (v.len() - info_i - 1) as u32;
        v.extend([
            3,
            0x0001_0000, // port 1, two hits
            (5 << 17) | (3 << 13) | 100,
            (7 << 17) | (15 << 13) | 8191,
            3,
            0x0003_0000, // port 3, two DCRB words
            0xDEAD,
            0xBEEF,
            1,
            0x0005_0001, // port 5, empty, unmapped
        ]);
        v[roc_start] = (v.len() - roc_start - 1) as u32;
        v[0] = (v.len() - 1) as u32;
        v
    }

    fn le(words: &[u32]) -> Vec<u8> {
        words.iter().flat_map(|w| w.to_le_bytes()).collect()
    }

    #[test]
    fn decodes_a_frame() {
        let bytes = le(&frame_words());
        let mut decoder = FrameDecoder::new();
        let frame = decoder
            .decode(Words::new(&bytes, ByteOrder::Little))
            .expect("stream frame");
        assert_eq!(frame.frame_number, 42);
        assert_eq!(frame.timestamp, 0x2_0000_1000);
        assert_eq!(frame.n_ais_entries, 3);
        assert_eq!(frame.n_ais_distinct, 1);
        assert_eq!(frame.n_errors, 0, "{:?}", frame.errors);
        assert_eq!(frame.roc_banks.len(), 1);
        let roc = frame.roc_banks[0];
        assert_eq!(roc.roc, 7);
        assert_eq!(roc.stream, 1);
        assert_eq!(roc.module, MODULE_FADC250);
        assert_eq!(roc.n_payload_banks, 3);
        assert_eq!(roc.n_empty_payload_banks, 1);
        assert_eq!(roc.n_unmapped_payload_banks, 1);
        assert_eq!(roc.n_hits, 2);
        assert_eq!(roc.non_fadc_words, 2);
        assert_eq!(frame.n_payload_words, 4);
        let hits: Vec<Hit> = frame.hits().collect();
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].channel, 3);
        assert_eq!(hits[0].time_raw, 5);
        assert_eq!(hits[0].charge, 100);
        assert_eq!(hits[0].port, 1);
        assert_eq!(hits[1].channel, 15);
        assert_eq!(hits[1].charge, 8191);
        assert_eq!(hits[1].t_ns(), 28);
        assert!(!hits[1].flag);
        let banks: Vec<_> = frame.payload_banks().collect();
        assert_eq!(banks.len(), 3);
        assert!(banks[2].is_empty());
        assert!(!banks[1].is_fadc250());
    }

    #[test]
    fn non_frame_and_bad_lengths() {
        let mut decoder = FrameDecoder::new();
        let other = le(&[1, 0x0001_0100, 5]);
        assert!(decoder
            .decode(Words::new(&other, ByteOrder::Little))
            .is_none());
        assert!(decoder
            .decode(Words::new(&other[..4], ByteOrder::Little))
            .is_none());

        // ROC bank length past the event end
        let mut words = frame_words();
        let roc_start = 12;
        words[roc_start] = 1000;
        let bytes = le(&words);
        let frame = decoder
            .decode(Words::new(&bytes, ByteOrder::Little))
            .unwrap();
        assert_eq!(frame.n_errors, 1);
        assert_eq!(frame.roc_banks.len(), 0);
        assert_eq!(frame.hits().count(), 0);

        // payload bank length 0xFFFFFFFF
        let mut words = frame_words();
        let last_bank = words.len() - 2;
        words[last_bank] = u32::MAX;
        let bytes = le(&words);
        let frame = decoder
            .decode(Words::new(&bytes, ByteOrder::Little))
            .unwrap();
        assert_eq!(frame.n_errors, 1);
        assert_eq!(frame.n_hits, 2);

        // event cut mid-bank: the length word says more than present
        let words = frame_words();
        let bytes = le(&words);
        let frame = decoder
            .decode(Words::new(&bytes[..bytes.len() - 8], ByteOrder::Little))
            .unwrap();
        assert!(frame.n_errors >= 1);
    }
}
