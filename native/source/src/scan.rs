//! What one chunk's lines say on their own: indent, the kind of statement a
//! line opens, and where triple quoted strings open and close.
//!
//! Everything here is a function of the chunk's text (plus, for strings,
//! the string state it is entered in), so it is computed once per chunk and
//! an edit rescans only the chunks it rewrote. The rules are the ones of the
//! Python fold scanner (`text_editor._scope_fold_ranges`): line based, never
//! failing on broken code.
use std::sync::OnceLock;

use crate::bind::{Binder, Bound};
use crate::store::Chunk;

pub const BLANK: u16 = 1;
pub const COMMENT: u16 = 2;
/// A `# [...]` metadata comment.
pub const META: u16 = 4;
pub const DEF: u16 = 8;
pub const CLASS: u16 = 16;
/// A compound statement header: `KEYWORDS[kw - 1]`.
pub const BLOCK: u16 = 32;
pub const IMPORT: u16 = 64;
/// The line ends in a backslash.
pub const SLASH: u16 = 128;
/// The line holds a quote: its string state needs a real scan.
pub const QUOTE: u16 = 256;

pub const KEYWORDS: [&str; 11] =
    ["if", "elif", "else", "for", "while", "try", "except", "finally", "with", "match", "case"];

pub struct Line {
    /// Byte range in the chunk, without the line break.
    pub start: u32,
    pub end: u32,
    /// Leading whitespace characters, and how many of them are spaces.
    pub indent: u32,
    pub spaces: u32,
    /// '(' minus ')' on the line.
    pub parens: i32,
    pub flags: u16,
    pub kw: u8,
}

pub struct Scan {
    pub lines: Vec<Line>,
    /// (line, name) of each `def` / `class` header.
    pub names: Vec<(u32, Box<str>)>,
}

/// Token kinds. An `OP` is one character of punctuation, an `OPS` an
/// operator of several (`==`, `:=`, `->`, `+=`).
pub const NAME: u8 = 0;
pub const KEYWORD: u8 = 1;
pub const NUMBER: u8 = 2;
pub const STRING: u8 = 3;
pub const NOTE: u8 = 4;
pub const OP: u8 = 5;
pub const OPS: u8 = 6;

/// One token: bytes `start .. start + len` of the chunk, on line `line` of it.
#[derive(Clone, Copy)]
pub struct Tok {
    pub start: u32,
    pub len: u16,
    pub line: u8,
    pub kind: u8,
}

/// What the lexer found in a chunk entered in one string state. Per line:
/// 1 it began inside a string, 2 that string closed on it, then the string
/// open at its end (`<< 2`). A string state is 0 in code, else 1 + 2 * kind
/// + quote: kind 0 a docstring (no prefix), 1 a prefixed string, 2 an
/// f-string; quote 0 `"""`, 1 `'''`. `toks` are the
/// tokens of every line, `first[i]` the index of line i's first one.
/// `calls[i]`: line i starts right inside the parentheses of a call that
/// opened on a line above it in this chunk (what `colour::runs` tells a
/// keyword argument at a line's start by; a call opened in the chunk above
/// is not known here, and its arguments then paint plain).
pub struct Strings {
    pub calls: Vec<bool>,
    pub lines: Vec<u8>,
    pub exit: u8,
    pub toks: Vec<Tok>,
    pub first: Vec<u32>,
    /// What the lines bind when no statement is open above them (`Chunk::bound`).
    bound: OnceLock<Bound>,
}

impl Strings {
    /// The tokens of line `line`.
    pub fn on(&self, line: usize) -> &[Tok] {
        &self.toks[self.first[line] as usize..self.first[line + 1] as usize]
    }
}

const KEYWORDS_ALL: [&str; 35] = [
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del",
    "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
    "not", "or", "pass", "raise", "return", "try", "while", "with", "yield"];

pub fn keyword(text: &str) -> bool {
    KEYWORDS_ALL.binary_search(&text).is_ok()
}

/// What Python's `str.strip` removes.
pub fn space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

pub fn strip(text: &str) -> &str {
    text.trim_matches(space)
}

fn word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// `text` after `prefix` and at least one whitespace character.
fn after<'a>(text: &'a str, prefix: &str) -> Option<&'a str> {
    let rest = text.strip_prefix(prefix)?;
    if rest.starts_with(space) { Some(rest.trim_start_matches(space)) } else { None }
}

/// (flag, name) when the stripped line opens a `def` or `class`.
fn scope(text: &str) -> Option<(u16, &str)> {
    let text = after(text, "async").unwrap_or(text);
    let (flag, rest) = match (after(text, "def"), after(text, "class")) {
        (Some(rest), _) => (DEF, rest),
        (_, Some(rest)) => (CLASS, rest),
        _ => return None,
    };
    let end = rest.find(|c| !word(c)).unwrap_or(rest.len());
    Some((flag, if end > 0 { &rest[..end] } else { "?" }))
}

/// The keyword index + 1 when the stripped line is a compound statement
/// header: keyword first, a ':' last before any trailing comment.
fn block(text: &str) -> u8 {
    let text = after(text, "async").unwrap_or(text);
    for (at, keyword) in KEYWORDS.iter().enumerate() {
        let Some(rest) = text.strip_prefix(keyword) else { continue };
        if rest.starts_with(word) { continue; }
        let closes = rest.match_indices(':').any(|(colon, _)| {
            let tail = rest[colon + 1..].trim_start_matches(space);
            tail.is_empty() || tail.starts_with('#')
        });
        return if closes { at as u8 + 1 } else { 0 };
    }
    0
}

impl Chunk {
    pub fn scan(&self) -> &Scan {
        self.scan.get_or_init(|| {
            let text: &str = &self.text;
            let mut scan = Scan { lines: Vec::with_capacity(self.breaks as usize + 1), names: vec![] };
            let mut start = 0;
            while start < text.len() {
                let end = text[start..].find('\n').map_or(text.len(), |at| start + at);
                let raw = &text[start..end];
                let body = raw.trim_start_matches(space);
                let stripped = strip(body);
                let lead = &raw[..raw.len() - body.len()];
                let mut line = Line {
                    start: start as u32, end: end as u32,
                    indent: lead.chars().count() as u32,
                    spaces: (raw.len() - raw.trim_start_matches(' ').len()) as u32,
                    parens: 0, flags: 0, kw: 0,
                };
                for byte in raw.bytes() {
                    match byte {
                        b'(' => line.parens += 1,
                        b')' => line.parens -= 1,
                        b'\'' | b'"' => line.flags |= QUOTE,
                        _ => {}
                    }
                }
                if stripped.is_empty() {
                    line.flags |= BLANK;
                } else if stripped.starts_with('#') {
                    line.flags |= COMMENT;
                    if stripped.starts_with("# [") || stripped.starts_with("#[") { line.flags |= META; }
                } else {
                    if let Some((flag, name)) = scope(stripped) {
                        line.flags |= flag;
                        scan.names.push((scan.lines.len() as u32, name.into()));
                    } else {
                        line.kw = block(stripped);
                        if line.kw > 0 { line.flags |= BLOCK; }
                    }
                    if after(stripped, "import").is_some() || after(stripped, "from").is_some() {
                        line.flags |= IMPORT;
                    }
                }
                if stripped.ends_with('\\') { line.flags |= SLASH; }
                scan.lines.push(line);
                start = end + 1;
            }
            scan
        })
    }

    /// The tokens and string state of every line when the chunk starts in state `entry`.
    pub fn strings(&self, entry: u8) -> &Strings {
        self.strings[entry as usize].get_or_init(|| {
            let scan = self.scan();
            let mut open = entry;
            let mut found = Strings { calls: Vec::with_capacity(scan.lines.len()), lines: Vec::with_capacity(scan.lines.len()),
                                      exit: 0, toks: vec![], first: Vec::with_capacity(scan.lines.len() + 1), bound: OnceLock::new() };
            // The brackets open so far, by what opened them (`colour::CALL`, `colour::PARAMS`, 0), and what
            // stood before the last token: b'd' a `def`'s name, b'a' something that can be called, 0 neither.
            let (mut brackets, mut before, mut def): (Vec<u8>, u8, bool) = (vec![], 0, false);
            for (at, line) in scan.lines.iter().enumerate() {
                let from = found.toks.len();
                found.first.push(from as u32);
                found.calls.push(brackets.last() == Some(&crate::colour::CALL));
                let began = (open != 0) as u8;
                let (closed, exit) = lex(&self.text, line.start as usize, line.end as usize, at as u8, open, &mut found.toks);
                open = exit;
                found.lines.push(began | (closed as u8) << 1 | open << 2);
                for tok in &found.toks[from..] {
                    let byte = self.text.as_bytes()[tok.start as usize];
                    before = match tok.kind {
                        NAME => if def { b'd' } else { b'a' },
                        OP => match byte {
                            b'(' => { brackets.push(match before { b'd' => crate::colour::PARAMS, b'a' => crate::colour::CALL, _ => 0 }); 0 }
                            b'[' | b'{' => { brackets.push(0); 0 }
                            b')' | b']' | b'}' => { brackets.pop(); b'a' }
                            _ => 0,
                        },
                        NOTE => before,
                        _ => 0,
                    };
                    def = tok.kind == KEYWORD && tok.len == 3 && self.text[tok.start as usize..].starts_with("def");
                }
            }
            found.first.push(found.toks.len() as u32);
            found.exit = open;
            found
        })
    }

    /// What the chunk's lines bind when it starts in string state `entry`
    /// and no statement is open above it.
    pub fn bound(&self, entry: u8) -> &Bound {
        let strings = self.strings(entry);
        strings.bound.get_or_init(|| Bound::read(&self.text, self.scan(), strings, Binder::default()))
    }

    pub fn line(&self, at: usize) -> &str {
        let line = &self.scan().lines[at];
        &self.text[line.start as usize..line.end as usize]
    }
}

/// Lex one line (`text[from .. to]`) entered in string state `open`: push
/// its tokens and return (the entering string closed here, the string open
/// at the line's end). The one string model of the editor: a triple quoted
/// string runs until it closes, an ordinary one to its closing quote or the
/// line's end; a quote inside a string or after a '#' is inert. Never fails:
/// what it cannot place is punctuation.
fn lex(text: &str, from: usize, to: usize, line: u8, mut open: u8, toks: &mut Vec<Tok>) -> (bool, u8) {
    let bytes = &text.as_bytes()[from..to];
    macro_rules! push {
        ($start:expr, $end:expr, $kind:expr) => {
            toks.push(Tok { start: (from + $start) as u32, len: ($end - $start).min(u16::MAX as usize) as u16, line, kind: $kind })
        };
    }
    // A string's text is one token; an f-string's goes through `fstring` (its fields are code).
    macro_rules! string {
        ($start:expr, $body:expr, $inner:expr, $end:expr, $kind:expr, $raw:expr) => {
            if $kind == 2 {
                fstring(text, line, from + $start, from + $body, from + $inner, from + $end, $raw, 1, toks);
            } else if $end > $start {
                push!($start, $end, STRING);
            }
        };
    }
    let (mut at, mut closed) = (0, false);
    if open != 0 {
        let kind = (open - 1) / 2;
        let Some(end) = close(bytes, 0, if (open - 1) % 2 == 0 { b'"' } else { b'\'' }, 3) else {
            string!(0, 0, bytes.len(), bytes.len(), kind, false);
            return (false, open);
        };
        string!(0, 0, end - 3, end, kind, false);
        (at, closed, open) = (end, true, 0);
    }
    while at < bytes.len() {
        let byte = bytes[at];
        let start = at;
        if byte == b' ' || byte == b'\t' || byte == b'\r' || byte == 0x0c {
            at += 1;
        } else if byte == b'#' {
            push!(at, bytes.len(), NOTE);
            break;
        } else if byte == b'"' || byte == b'\'' {
            // A string takes the prefix letters right before it (r, b, f, u).
            let (start, kind) = match toks.last() {
                Some(last) if last.kind == NAME && last.line == line && (last.start + last.len as u32) as usize == from + at
                    && last.len <= 2 && text[last.start as usize..from + at].bytes().all(|c| b"rRbBfFuU".contains(&c)) => {
                    let start = last.start as usize - from;
                    toks.pop();
                    (start, if bytes[start..at].iter().any(|c| matches!(c, b'f' | b'F')) { 2 } else { 1 })
                }
                _ => (at, 0),
            };
            let raw = bytes[start..at].iter().any(|c| matches!(c, b'r' | b'R'));
            let size = if bytes[at..].starts_with(&[byte; 3]) { 3 } else { 1 };
            let body = at + size;
            match close(bytes, body.min(bytes.len()), byte, size) {
                Some(end) => {
                    string!(start, body, end - size, end, kind, raw);
                    at = end;
                }
                None => {
                    string!(start, body.min(bytes.len()), bytes.len(), bytes.len(), kind, raw);
                    if size == 3 { return (closed, 1 + 2 * kind + (byte == b'\'') as u8); }
                    at = bytes.len();
                }
            }
        } else if byte.is_ascii_alphabetic() || byte == b'_' || byte >= 0x80 {
            while at < bytes.len() && (bytes[at].is_ascii_alphanumeric() || bytes[at] == b'_' || bytes[at] >= 0x80) { at += 1; }
            push!(start, at, if keyword(&text[from + start..from + at]) { KEYWORD } else { NAME });
        } else if byte.is_ascii_digit() || (byte == b'.' && bytes.get(at + 1).is_some_and(u8::is_ascii_digit)) {
            while at < bytes.len() && (bytes[at].is_ascii_alphanumeric() || bytes[at] == b'_' || bytes[at] == b'.'
                || (matches!(bytes[at], b'+' | b'-') && matches!(bytes[at - 1], b'e' | b'E') && !bytes[start..].starts_with(b"0x"))) { at += 1; }
            push!(start, at, NUMBER);
        } else {
            // Operators of several characters: a repeat of one of `* / < >`, then an `=`; `->`, `...`.
            at += 1;
            if matches!(byte, b'*' | b'/' | b'<' | b'>') && bytes.get(at) == Some(&byte) { at += 1; }
            if bytes.get(at) == Some(&b'=') && b"=!<>:+-*/%&|^@".contains(&byte) { at += 1; }
            if byte == b'-' && bytes.get(at) == Some(&b'>') { at += 1; }
            if bytes[start..].starts_with(b"...") { at = start + 3; }
            push!(start, at, if at - start > 1 { OPS } else { OP });
        }
    }
    (closed, open)
}

/// Where the replacement field that opens at `open` (a `{`) of an f-string
/// ends, looking no further than `end`: (the end of its expression, its
/// closing `}` if it has one). The rules of `colour.rs`.
fn field(bytes: &[u8], open: usize, end: usize, nest: u8) -> (usize, Option<usize>) {
    let (mut k, mut depth) = (open + 1, 0i32);
    while k < end {
        match bytes[k] {
            b'(' | b'[' | b'{' => depth += 1,
            b')' | b']' => depth = (depth - 1).max(0),
            b'}' => {
                if depth == 0 { break; }
                depth -= 1;
            }
            quote @ (b'"' | b'\'') => {
                // A string inside the expression.
                let size = if bytes[k..].starts_with(&[quote; 3]) { 3 } else { 1 };
                k = close(&bytes[..end], k + size, quote, size).unwrap_or(end);
                continue;
            }
            b':' if depth == 0 => break,
            b'!' if bytes.get(k + 1) == Some(&b'=') => k += 1,
            b'!' if depth == 0 => break,
            _ => {}
        }
        k += 1;
    }
    let expression = k.min(end);
    // The conversion, then the format spec with fields of its own.
    while k < end && !matches!(bytes[k], b':' | b'}') { k += 1; }
    if k < end && bytes[k] == b':' {
        while k < end && bytes[k] != b'}' {
            k = match (bytes[k], nest < 4) {
                (b'{', true) => match field(bytes, k, end, nest + 1).1 {
                    Some(close) => close + 1,
                    None => end,
                },
                _ => k + 1,
            };
        }
    }
    (expression, (k < end && bytes[k] == b'}').then_some(k))
}

/// The tokens of an f-string, bytes `start .. end` of the chunk with its
/// text in `body .. inner`: literal text is string; a replacement field
/// that closes is `{`, the tokens of its expression, its conversion and
/// format spec (string, with fields of its own) and `}`. A field left open
/// is string, so only whole fields change what brackets are open.
#[allow(clippy::too_many_arguments)]
fn fstring(text: &str, line: u8, start: usize, body: usize, inner: usize, end: usize, raw: bool, nest: u8, toks: &mut Vec<Tok>) {
    let bytes = text.as_bytes();
    let push = |from: usize, to: usize, kind: u8, toks: &mut Vec<Tok>| {
        if to > from { toks.push(Tok { start: from as u32, len: (to - from).min(u16::MAX as usize) as u16, line, kind }); }
    };
    let (mut at, mut literal) = (body, start);
    while at < inner {
        let byte = bytes[at];
        if byte == b'\\' && !raw {
            // (`\N{NAME}` is a character, not a field.)
            at = if bytes[at + 1..].starts_with(b"N{") {
                bytes[at..inner].iter().position(|&byte| byte == b'}').map_or(inner, |close| at + close + 1)
            } else {
                at + if matches!(bytes.get(at + 1), Some(b'{') | Some(b'}')) { 1 } else { 2 }
            };
        } else if matches!(byte, b'{' | b'}') && at + 1 < inner && bytes[at + 1] == byte {
            at += 2;
        } else if byte == b'{' {
            let (expression, Some(close)) = field(bytes, at, inner, nest) else { break };
            push(literal, at, STRING, toks);
            push(at, at + 1, OP, toks);
            lex(text, at + 1, expression, line, 0, toks);
            let spec = bytes[expression..close].iter().position(|&byte| byte == b':').map_or(close, |colon| expression + colon + 1);
            fstring(text, line, expression, spec, close, close, true, nest + 1, toks);
            push(close, close + 1, OP, toks);
            (at, literal) = (close + 1, close + 1);
        } else {
            at += 1;
        }
    }
    push(literal, end, STRING, toks);
}

/// The offset after the next run of `count` `quote`s from `at` that no backslash escapes.
fn close(line: &[u8], mut at: usize, quote: u8, count: usize) -> Option<usize> {
    while at < line.len() {
        if line[at] == b'\\' && at + 1 < line.len() {
            at += 2;
        } else if line[at..].len() >= count && line[at..at + count].iter().all(|&byte| byte == quote) {
            return Some(at + count);
        } else {
            at += 1;
        }
    }
    None
}
