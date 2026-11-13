//! The text as chunks of whole lines.
//!
//! A chunk is immutable and shared: an edit replaces the chunks it touches,
//! a snapshot for the worker is a clone of the chunk list, and what is
//! derived from a chunk alone (`scan.rs`) is cached on it for as long as it
//! lives. Offsets are in characters, as Python strings index.
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use crate::facts::Lint;
use crate::scan::{Scan, Strings};

/// Lever: lines in a fresh chunk. A chunk is also one block of the editor
/// (one view): smaller chunks rescan and repaint less per edit, larger ones
/// make every pass over the chunk list shorter. An edited chunk is cut again
/// only when it holds more than twice this.
pub const LINES: usize = 32;
/// Lever: chunks the render thread may lex itself to learn a string state.
const LEX: usize = 8;

pub struct Chunk {
    /// Whole lines: every line ends in '\n' except the last of the text.
    pub text: Box<str>,
    pub chars: u32,
    pub breaks: u32,
    pub scan: OnceLock<Scan>,
    /// Tokens and string state per line, for each state the chunk can be entered in.
    pub strings: [OnceLock<Strings>; 7],
    /// What the lint last found in the chunk (`Facts::lint` keeps it while its key holds).
    pub lint: Mutex<Option<Arc<Lint>>>,
    /// Which chunk this is, among all ever made: what findings that depend on a neighbour are keyed by.
    pub id: u64,
}

static MADE: AtomicU64 = AtomicU64::new(0);

impl Chunk {
    pub fn new(text: &str) -> Arc<Chunk> {
        let chars = if text.is_ascii() { text.len() } else { text.chars().count() };
        let breaks = text.as_bytes().iter().filter(|&&byte| byte == b'\n').count();
        Arc::new(Chunk {
            text: text.into(), chars: chars as u32, breaks: breaks as u32,
            scan: OnceLock::new(), strings: Default::default(), lint: Mutex::new(None), id: MADE.fetch_add(1, Ordering::Relaxed),
        })
    }

    /// The byte offset of character `at`.
    pub fn byte(&self, at: usize) -> usize {
        if self.chars as usize == self.text.len() { return at; }
        self.text.char_indices().nth(at).map_or(self.text.len(), |(byte, _)| byte)
    }
}

/// Cut `text` into chunks of `LINES` lines; up to twice that stay one chunk.
fn chunks(text: &str) -> Vec<Arc<Chunk>> {
    let mut cuts = vec![0];
    let mut lines = 0;
    for (at, _) in text.match_indices('\n') {
        lines += 1;
        if lines % LINES == 0 { cuts.push(at + 1); }
    }
    if lines <= LINES * 2 { cuts.truncate(1); }
    if *cuts.last().unwrap() < text.len() { cuts.push(text.len()); }
    cuts.windows(2).map(|cut| Chunk::new(&text[cut[0]..cut[1]])).collect()
}

pub struct Store {
    pub chunks: Vec<Arc<Chunk>>,
    /// Per chunk: a key that stays with it through edits (an edit's first
    /// chunk keeps it), and the stamp of the edit that last rewrote it.
    pub keys: Vec<u64>,
    pub stamps: Vec<u64>,
    serial: u64,
    /// Characters before chunk i, for the chunks summed so far.
    char0: Vec<u32>,
    /// Lines before chunk i and the string state it is entered in, as far as known.
    line0: Vec<u32>,
    entry: Vec<u8>,
    pub chars: usize,
    pub breaks: usize,
}

impl Store {
    pub fn new(text: &str) -> Store {
        let mut chunks = chunks(text);
        if chunks.is_empty() { chunks.push(Chunk::new("")); }
        let chars = chunks.iter().map(|chunk| chunk.chars as usize).sum();
        let breaks = chunks.iter().map(|chunk| chunk.breaks as usize).sum();
        let serial = chunks.len() as u64;
        Store { keys: (1..=serial).collect(), stamps: vec![0; chunks.len()], serial, chunks,
                char0: vec![], line0: vec![], entry: vec![], chars, breaks }
    }

    /// The chunk holding character `at` (the last one for the text's end).
    /// `char0` is summed only as far as `at`, so an edit never pays for the
    /// chunks below it.
    fn chunk(&mut self, at: usize) -> usize {
        while self.char0.len() < self.chunks.len() {
            let next = match self.char0.len() {
                0 => 0,
                n => self.char0[n - 1] + self.chunks[n - 1].chars,
            };
            if !self.char0.is_empty() && next as usize > at { break; }
            self.char0.push(next);
        }
        self.char0.partition_point(|&start| start as usize <= at) - 1
    }

    /// Put `new` in place of characters `low .. high`; the chunks rewritten get `stamp`.
    pub fn splice(&mut self, low: usize, high: usize, new: &str, stamp: u64) {
        let low = low.min(self.chars);
        let high = high.clamp(low, self.chars);
        let a = self.chunk(low);
        let mut b = self.chunk(high);
        let head = &self.chunks[a];
        let tail = &self.chunks[b];
        let cut = head.byte(low - self.char0[a] as usize);
        let rest = tail.byte(high - self.char0[b] as usize);
        let mut text = String::with_capacity(cut + new.len() + tail.text.len() - rest);
        text.push_str(&head.text[..cut]);
        text.push_str(new);
        text.push_str(&tail.text[rest..]);
        // A chunk left with few lines joins the next one, so deleting never
        // leaves a trail of tiny chunks.
        let breaks = text.bytes().filter(|&byte| byte == b'\n').count();
        if breaks < LINES / 4 && b + 1 < self.chunks.len() {
            b += 1;
            text.push_str(&self.chunks[b].text);
        }
        let mut fresh = chunks(&text);
        if fresh.is_empty() && a == 0 && b + 1 == self.chunks.len() {
            fresh.push(Chunk::new(""));
        }
        let (mut chars, mut lines) = (0usize, 0usize);
        for old in &self.chunks[a..=b] {
            chars += old.chars as usize;
            lines += old.breaks as usize;
        }
        self.chars -= chars;
        self.breaks -= lines;
        for chunk in &fresh {
            self.chars += chunk.chars as usize;
            self.breaks += chunk.breaks as usize;
        }
        let mut keys = vec![self.keys[a]];
        while keys.len() < fresh.len() {
            self.serial += 1;
            keys.push(self.serial);
        }
        keys.truncate(fresh.len());
        self.stamps.splice(a..=b, vec![stamp; fresh.len()]);
        self.keys.splice(a..=b, keys);
        self.chunks.splice(a..=b, fresh);
        self.char0.truncate(a + 1);
        self.line0.truncate(a + 1);
        self.entry.truncate(a + 1);
    }

    /// (chunk, line in it) of line `line` (the last chunk for lines past the end).
    pub fn line(&mut self, line: usize) -> (usize, usize) {
        while self.line0.len() < self.chunks.len() {
            let next = match self.line0.len() {
                0 => 0,
                n => self.line0[n - 1] + self.chunks[n - 1].breaks,
            };
            if !self.line0.is_empty() && next as usize > line { break; }
            self.line0.push(next);
        }
        let at = self.line0.partition_point(|&start| start as usize <= line) - 1;
        (at, line - self.line0[at] as usize)
    }

    /// Characters before chunk `at`.
    pub fn chars_before(&mut self, at: usize) -> usize {
        while self.char0.len() <= at {
            let next = match self.char0.len() {
                0 => 0,
                n => self.char0[n - 1] + self.chunks[n - 1].chars,
            };
            self.char0.push(next);
        }
        self.char0[at] as usize
    }

    /// Lines before chunk `at`.
    pub fn lines_before(&mut self, at: usize) -> usize {
        while self.line0.len() <= at {
            let next = match self.line0.len() {
                0 => 0,
                n => self.line0[n - 1] + self.chunks[n - 1].breaks,
            };
            self.line0.push(next);
        }
        self.line0[at] as usize
    }

    /// (line, column) of character `at`.
    pub fn place(&mut self, at: usize) -> (usize, usize) {
        let at = at.min(self.chars);
        let index = self.chunk(at);
        let (chunk, base) = (self.chunks[index].clone(), self.lines_before(index));
        let byte = chunk.byte(at - self.char0[index] as usize);
        let lines = &chunk.scan().lines;
        let Some(local) = lines.partition_point(|line| line.start as usize <= byte).checked_sub(1) else { return (base, 0) };
        // Past a line's break: the next line's start (the text's last, empty line).
        if byte > lines[local].end as usize { return (base + local + 1, 0); }
        (base + local, chunk.text[lines[local].start as usize..byte].chars().count())
    }

    /// The character offset line `line` starts at (the text's end for lines past it).
    pub fn start(&mut self, line: usize) -> usize {
        let (index, local) = self.line(line);
        let chunk = self.chunks[index].clone();
        let base = self.chars_before(index);
        match chunk.scan().lines.get(local) {
            Some(found) if chunk.chars as usize == chunk.text.len() => base + found.start as usize,
            Some(found) => base + chunk.text[..found.start as usize].chars().count(),
            None => base + chunk.chars as usize,
        }
    }

    /// The text of characters `low .. high`.
    pub fn slice(&mut self, low: usize, high: usize) -> String {
        let (low, high) = (low.min(self.chars), high.min(self.chars));
        if high <= low { return String::new(); }
        let (a, b) = (self.chunk(low), self.chunk(high));
        let mut text = String::new();
        for index in a..=b {
            let chunk = &self.chunks[index];
            let from = if index == a { chunk.byte(low - self.char0[a] as usize) } else { 0 };
            let to = if index == b { chunk.byte(high - self.char0[b] as usize) } else { chunk.text.len() };
            text.push_str(&chunk.text[from..to]);
        }
        text
    }

    /// (text, where it starts): the text around character `at`, from at
    /// least `reach` characters before it to the end of the chunk after its own.
    pub fn near(&mut self, at: usize, reach: usize) -> (String, usize) {
        let index = self.chunk(at.min(self.chars));
        let mut low = index;
        while low > 0 && at - (self.char0[low] as usize) < reach { low -= 1; }
        let high = (index + 1).min(self.chunks.len() - 1);
        (self.chunks[low..=high].iter().map(|chunk| &*chunk.text).collect(), self.char0[low] as usize)
    }

    /// The string state chunk `at` is entered in. Chunks nobody lexed yet
    /// are lexed here, at most `LEX` of them: beyond that the state is not
    /// known (None) until a reading has gone over the text.
    pub fn entry(&mut self, at: usize) -> Option<u8> {
        if self.entry.is_empty() { self.entry.push(0); }
        let mut budget = LEX;
        while self.entry.len() <= at {
            let before = self.entry.len() - 1;
            let (chunk, state) = (&self.chunks[before], self.entry[before]);
            if chunk.strings[state as usize].get().is_none() {
                if budget == 0 { return None; }
                budget -= 1;
            }
            self.entry.push(chunk.strings(state).exit);
        }
        Some(self.entry[at])
    }

    pub fn text(&self) -> String {
        let mut text = String::with_capacity(self.chunks.iter().map(|chunk| chunk.text.len()).sum());
        for chunk in &self.chunks { text.push_str(&chunk.text); }
        text
    }
}
