//! A minimal terminal viewer for EVIO v6 files and JLab SRO streams, built
//! on the `evio-stream` reader: a fast way to look inside a file from a
//! terminal, nothing more.
//!
//! The binary `evio-tui FILE` scans the file on a background thread
//! ([`scan`]) and shows the records as they are found, opens one record by
//! seeking to its offset ([`access`]), lists its events, and shows the bank
//! tree and the words of one event, with the fADC250 frame summary of a
//! stream frame.
//!
//! Modules: [`state`] (what is shown and selected; keys), [`format`] (the
//! text of every cell and panel), [`tree`] (the flattened bank tree), and
//! [`ui`] (drawing with ratatui). Everything but `ui` runs without a
//! terminal.

#![forbid(unsafe_code)]

pub mod access;
pub mod format;
pub mod scan;
pub mod state;
pub mod tree;
pub mod ui;
