//! The colour runs of one line: what the editor paints a line's text with.
//!
//! `runs(line, state, call)` is a function of the line's text, the string it
//! starts inside (`state`, from the chunk lexer in `scan.rs`: the one string
//! model) and whether it starts right inside a call's parentheses (`call`:
//! `Strings::calls`, what a keyword argument at the start of a continuation
//! line is told by). The kinds are the editor's colour keys (`KINDS`); neighbours of
//! one plain kind come out as one run. The rules are the legacy tokenizer's
//! (`text_editor._tokenize_from`): words by role, numbers with their sign,
//! `(r, g, b)` tuples of floats as one colour token, hex colour strings,
//! icon glyphs, f-string fields as code, values in `# [key=value]` comments;
//! and, beyond the legacy rules, the name of a keyword argument in a call
//! (`kwarg`: never a parameter's default in a `def` or a `lambda`).
//! To add a kind: name it in `KINDS`, emit it in `raw`.
use std::collections::HashSet;
use std::sync::OnceLock;

pub const KINDS: [&str; 19] = [
    "default", "comment", "string", "string_doc", "keyword", "operator_word", "def", "def_name", "keyword_const", "bool",
    "builtin_pseudo", "builtin", "decorator", "number", "color3", "colorhex", "icon", "fstring_delim", "kwarg"];
const DEFAULT: u8 = 0;
const COMMENT: u8 = 1;
const STRING: u8 = 2;
const DOC: u8 = 3;
const KEYWORD: u8 = 4;
const WORD_OP: u8 = 5;
const DEF: u8 = 6;
const DEF_NAME: u8 = 7;
const CONST: u8 = 8;
const BOOL: u8 = 9;
const PSEUDO: u8 = 10;
const BUILTIN: u8 = 11;
const DECORATOR: u8 = 12;
const NUMBER: u8 = 13;
const COLOR3: u8 = 14;
const HEX: u8 = 15;
const ICON: u8 = 16;
const DELIM: u8 = 17;
const KWARG: u8 = 18;
/// What a bracket was opened as (`raw`, and `scan::Strings::calls` with the same values).
pub const CALL: u8 = 1;
pub const PARAMS: u8 = 2;
/// Kinds below this join their neighbours of the same kind into one run.
const JOINED: u8 = 6;

const KEYWORDS: [&str; 27] = [
    "def", "class", "if", "else", "elif", "for", "while", "return", "import", "from", "with", "as", "try", "except",
    "finally", "raise", "yield", "pass", "break", "continue", "lambda", "global", "nonlocal", "del", "assert", "async",
    "await"];

/// The names coloured as builtins: given by Python once (`builtins`), so
/// they are the running interpreter's.
pub static BUILTINS: OnceLock<HashSet<String>> = OnceLock::new();

/// Bytes `.0 .. .1` of the line, of kind `.2`.
type Piece = (usize, usize, u8);

fn icon(c: char) -> bool {
    ('\u{e000}'..='\u{f8ff}').contains(&c)
}

fn at(s: &str, i: usize) -> char {
    s[i..].chars().next().unwrap_or('\0')
}

/// A string's text in `base`, its icon glyphs apart; a hex colour string is its own kind.
fn icons(s: &str, a: usize, b: usize, base: u8, out: &mut Vec<Piece>) {
    let text = &s[a..b];
    let bytes = text.as_bytes();
    if base == STRING && matches!(bytes.len(), 6 | 9 | 11) && matches!(bytes[0], b'"' | b'\'') && bytes[1] == b'#'
        && bytes[bytes.len() - 1] == bytes[0] && bytes[2..bytes.len() - 1].iter().all(u8::is_ascii_hexdigit) {
        return out.push((a, b, HEX));
    }
    let mut start = a;
    let mut run: Option<bool> = None;
    for (index, c) in text.char_indices() {
        let is = icon(c);
        if run.is_some_and(|run| run != is) {
            out.push((start, a + index, if is { base } else { ICON }));
            start = a + index;
        }
        run = Some(is);
    }
    if start < b { out.push((start, b, if run == Some(true) { ICON } else { base })); }
}

/// The literal run `a .. b` of an f-string.
fn literal(s: &str, a: usize, b: usize, out: &mut Vec<Piece>) {
    if a >= b { return; }
    if s[a..b].chars().any(icon) { icons(s, a, b, STRING, out); } else { out.push((a, b, STRING)); }
}

/// The index past the string that opens at `k` inside an f-string field.
fn quoted(s: &str, mut k: usize, end: usize) -> usize {
    let bytes = s.as_bytes();
    let triple = bytes[k..].starts_with(&[bytes[k]; 3]);
    let size = if triple { 3 } else { 1 };
    let quote = &bytes[k..k + size];
    k += size;
    while k < end {
        if bytes[k] == b'\\' && !triple {
            k += 2;
        } else if bytes[k..].starts_with(quote) {
            return end.min(k + size);
        } else {
            k += 1;
        }
    }
    end
}

/// One replacement field of an f-string from its `{` at `i`: the braces,
/// the conversion and the `:` are delimiters, the expression is code, the
/// format spec is string with fields of its own. Returns the index after it.
fn field(s: &str, i: usize, end: usize, out: &mut Vec<Piece>, nest: u8) -> usize {
    let bytes = s.as_bytes();
    out.push((i, i + 1, DELIM));
    let (mut k, mut depth) = (i + 1, 0i32);
    while k < end {
        match bytes[k] {
            b'(' | b'[' | b'{' => depth += 1,
            b')' | b']' => depth = (depth - 1).max(0),
            b'}' => {
                if depth == 0 { break; }
                depth -= 1;
            }
            b'"' | b'\'' => {
                k = quoted(s, k, end);
                continue;
            }
            b':' if depth == 0 => break,
            b'!' => {
                if bytes.get(k + 1) == Some(&b'=') { k += 1; } else if depth == 0 { break; }
            }
            _ => {}
        }
        k += 1;
    }
    k = k.min(end);
    if k > i + 1 { raw(s, i + 1, k, false, out); }
    if k < end && bytes[k] == b'!' {
        let mut m = k + 1;
        while m < end && (at(s, m).is_alphanumeric() || bytes[m] == b'_') { m += at(s, m).len_utf8(); }
        out.push((k, m, DELIM));
        k = m;
        while m < end && !matches!(bytes[m], b':' | b'}') { m += 1; }
        if m > k {
            out.push((k, m, DEFAULT));
            k = m;
        }
    }
    if k < end && bytes[k] == b':' {
        out.push((k, k + 1, DELIM));
        k += 1;
        let mut lit = k;
        while k < end && bytes[k] != b'}' {
            if bytes[k] == b'{' && nest < 4 {
                literal(s, lit, k, out);
                k = field(s, k, end, out, nest + 1);
                lit = k;
            } else {
                k += 1;
            }
        }
        literal(s, lit, k, out);
    }
    if k < end && bytes[k] == b'}' {
        out.push((k, k + 1, DELIM));
        k += 1;
    }
    k
}

/// An f-string `a .. b`: literal text is string, each replacement field
/// goes through `field`. `body` is where the text after the opening quote
/// starts, `quote` the closing quote.
fn fstring(s: &str, a: usize, b: usize, quote: &str, body: usize, verbatim: bool, out: &mut Vec<Piece>) {
    let bytes = s.as_bytes();
    let end = if b - a >= body - a + quote.len() && s[a..b].ends_with(quote) { b - quote.len() } else { b };
    let (mut i, mut lit) = (body, a);
    while i < end {
        let c = bytes[i];
        if c == b'\\' && !verbatim {
            if s[i + 1..].starts_with("N{") {
                i = s[..end].get(i + 3..).and_then(|rest| rest.find('}')).map_or(end, |close| i + 3 + close + 1);
            } else {
                i += if matches!(bytes.get(i + 1), Some(b'{') | Some(b'}')) { 1 } else { 2 };
            }
        } else if matches!(c, b'{' | b'}') && i + 1 < end && bytes[i + 1] == c {
            i += 2;
        } else if c == b'{' {
            literal(s, lit, i, out);
            i = field(s, i, end, out, 1);
            lit = i;
        } else {
            i += 1;
        }
    }
    literal(s, lit.min(b), b, out);
}

/// The index after the ordinary string whose text starts at `from`: past
/// its closing `quote`, or the end.
fn closing(bytes: &[u8], mut from: usize, to: usize, quote: u8) -> usize {
    while from < to {
        if bytes[from] == b'\\' {
            from += 2;
        } else if bytes[from] == quote {
            return from + 1;
        } else {
            from += 1;
        }
    }
    to
}

/// The index after the number that starts at `i`.
fn number(bytes: &[u8], i: usize, to: usize) -> usize {
    let mut end = i;
    if end + 1 < to && bytes[end] == b'0' && b"xXoObB".contains(&bytes[end + 1]) {
        end += 2;
        while end < to && (bytes[end].is_ascii_hexdigit() || bytes[end] == b'_') { end += 1; }
        return end;
    }
    let mut dot = false;
    while end < to && (bytes[end].is_ascii_digit() || b"._eE".contains(&bytes[end])) {
        if bytes[end] == b'.' {
            if dot { break; }
            dot = true;
        }
        if matches!(bytes[end], b'e' | b'E') && end + 1 < to && matches!(bytes[end + 1], b'+' | b'-') { end += 1; }
        end += 1;
    }
    end.min(to)
}

/// The values of a `# [key=value]` comment keep their kinds; the rest is comment.
fn overrides(s: &str, a: usize, b: usize, out: &mut Vec<Piece>) {
    let bytes = s.as_bytes();
    let mut split = a + 1;
    while split < b && matches!(bytes[split], b' ' | b'\t') { split += 1; }
    out.push((a, split, COMMENT));
    let mut inner = vec![];
    raw(s, split, b, false, &mut inner);
    for (from, to, kind) in tuples(s, signs(s, inner, true), true) {
        out.push((from, to, if matches!(kind, BOOL | NUMBER | COLOR3 | ICON) { kind } else { COMMENT }));
    }
}

/// Whether a comment is `#`, an optional `[`, then `key=` (not `==`). The
/// key starts with an ASCII letter or `_`, as the legacy tokenizer has it.
fn overriding(text: &str) -> bool {
    let rest = text[1..].trim_start();
    let rest = rest.strip_prefix('[').unwrap_or(rest).trim_start();
    let name = rest.find(|c: char| !(c.is_alphanumeric() || c == '_')).unwrap_or(rest.len());
    rest.starts_with(|c: char| c.is_ascii_alphabetic() || c == '_') && rest[name..].starts_with('=') && !rest[name..].starts_with("==")
}

/// The tokens of code `from .. to`, before the merges.
fn raw(s: &str, from: usize, to: usize, call: bool, out: &mut Vec<Piece>) {
    let bytes = s.as_bytes();
    let (mut i, mut after_def) = (from, false);
    // For keyword argument names: the brackets open here (0 any, `CALL` the parentheses of a call,
    // `PARAMS` those of a `def`), the last character that was no space (b'a': a word, a literal),
    // and whether a `lambda`'s parameters are being read.
    let mut open: Vec<u8> = if call { vec![CALL] } else { vec![] };
    let (mut last, mut lambda) = (if call { b',' } else { 0u8 }, false);
    while i < to {
        let c = at(s, i);
        let before = last;
        if !matches!(c, ' ' | '\t') { last = b'a'; }
        let prefixed = b"fFrRbBuU".contains(&bytes[i]) && i + 1 < to
            && (matches!(bytes[i + 1], b'"' | b'\'') || (i + 2 < to && b"fFrRbBuU".contains(&bytes[i + 1]) && matches!(bytes[i + 2], b'"' | b'\'')));
        if c == '#' {
            if overriding(&s[i..to]) { overrides(s, i, to, out); } else { out.push((i, to, COMMENT)); }
            i = to;
        } else if c == '@' && (i == from || bytes[i - 1] == b' ') {
            let mut end = i + 1;
            while end < to && (at(s, end).is_alphanumeric() || matches!(bytes[end], b'_' | b'.')) { end += at(s, end).len_utf8(); }
            out.push((i, end, DECORATOR));
            i = end;
        } else if i + 2 < to && matches!(c, '"' | '\'') && bytes[i..].starts_with(&[bytes[i]; 3]) {
            let end = s[i + 3..to].find(&s[i..i + 3]).map_or(to, |close| i + 3 + close + 3);
            icons(s, i, end, DOC, out);
            i = end;
        } else if prefixed {
            let start = i + 1 + b"fFrRbBuU".contains(&bytes[i + 1]) as usize;
            let quote = bytes[start];
            let triple = start + 2 < to && bytes[start..].starts_with(&[quote; 3]);
            let end = if triple {
                s[start + 3..to].find(&s[start..start + 3]).map_or(to, |close| start + 3 + close + 3)
            } else {
                closing(bytes, start + 1, to, quote).min(to)
            };
            let prefix = s[i..start].to_ascii_lowercase();
            if prefix.contains('f') {
                let size = if s[start..end].len() >= 3 && bytes[start..].starts_with(&[quote; 3]) { 3 } else { 1 };
                fstring(s, i, end, &s[start..start + size.min(end - start)], start + size.min(end - start), prefix.contains('r'), out);
            } else {
                icons(s, i, end, STRING, out);
            }
            i = end;
        } else if matches!(c, '"' | '\'') {
            let end = closing(bytes, i + 1, to, bytes[i]).min(to);
            icons(s, i, end, STRING, out);
            i = end;
        } else if c.is_alphabetic() || c == '_' {
            let mut end = i;
            while end < to && (at(s, end).is_alphanumeric() || bytes[end] == b'_') { end += at(s, end).len_utf8(); }
            let word = &s[i..end];
            out.push((i, end, match word {
                "True" | "False" => BOOL,
                "None" => CONST,
                "and" | "or" | "not" | "in" | "is" => WORD_OP,
                "def" => DEF,
                _ if KEYWORDS.contains(&word) => KEYWORD,
                "self" | "cls" => PSEUDO,
                _ if after_def => DEF_NAME,
                // `name=` right after the `(` or a `,` of a call: a keyword argument, whatever else the name is.
                _ if open.last() == Some(&CALL) && !lambda && matches!(before, b'(' | b',') && {
                    let rest = s[end..to].trim_start_matches(' ').as_bytes();
                    rest.first() == Some(&b'=') && rest.get(1) != Some(&b'=')
                } => KWARG,
                _ if !(i > from && bytes[i - 1] == b'.') && BUILTINS.get().is_some_and(|names| names.contains(word)) => BUILTIN,
                _ => DEFAULT,
            }));
            lambda |= word == "lambda";
            // (A keyword before a parenthesis groups, it does not call: `return (`, `in (`.)
            last = if after_def { b'd' } else if out.last().is_some_and(|piece| matches!(piece.2, KEYWORD | WORD_OP)) { b'k' } else { b'a' };
            after_def = word == "def";
            i = end;
        } else if c.is_ascii_digit() || (c == '.' && i + 1 < to && bytes[i + 1].is_ascii_digit()) {
            let end = number(bytes, i, to);
            out.push((i, end, NUMBER));
            i = end;
        } else {
            if !matches!(c, ' ' | '\t') { after_def = false; }
            match c {
                '(' => open.push(match before { b'd' => PARAMS, b'a' | b')' | b']' => CALL, _ => 0 }),
                '[' | '{' => open.push(0),
                ')' | ']' | '}' => { open.pop(); }
                ':' => lambda = false,
                _ => {}
            }
            if !matches!(c, ' ' | '\t') { last = if c.is_ascii() { c as u8 } else { b'a' }; }
            out.push((i, i + c.len_utf8(), if icon(c) { ICON } else { DEFAULT }));
            i += c.len_utf8();
        }
    }
}

/// Whether a `+` or `-` after this token is a sign, and a `(` a tuple's.
fn unary(s: &str, piece: Piece) -> bool {
    let text = &s[piece.0..piece.1];
    match piece.2 {
        KEYWORD | WORD_OP => true,
        DEFAULT => text.len() == 1 && "=+-*/%<>&|^~,([{:;@".contains(text),
        DELIM => text == "{",
        _ => false,
    }
}

fn blank(s: &str, piece: Piece) -> bool {
    s[piece.0..piece.1].chars().all(char::is_whitespace)
}

/// A `+` or `-` right before a number, where it reads as a sign, joins the number.
fn signs(s: &str, pieces: Vec<Piece>, mut sign: bool) -> Vec<Piece> {
    let mut out: Vec<Piece> = Vec::with_capacity(pieces.len());
    let mut held: Option<Piece> = None;
    for piece in pieces {
        if let Some(sign_piece) = held.take() {
            if piece.2 == NUMBER {
                out.push((sign_piece.0, piece.1, NUMBER));
                sign = false;
                continue;
            }
            out.push(sign_piece);
            sign = unary(s, sign_piece);
        }
        if piece.2 == DEFAULT && matches!(&s[piece.0..piece.1], "+" | "-") && sign {
            held = Some(piece);
            continue;
        }
        out.push(piece);
        if !blank(s, piece) && piece.2 != COMMENT { sign = unary(s, piece); }
    }
    out.extend(held);
    out
}

/// `(c, c, c)` or `(c, c, c, c)` of floats (and the ints 0 and 1, at least
/// one float), where a tuple can stand, becomes one colour token.
fn tuples(s: &str, pieces: Vec<Piece>, mut open: bool) -> Vec<Piece> {
    let float = |piece: Piece| {
        let text = s[piece.0..piece.1].trim_start_matches(['+', '-']).to_ascii_lowercase();
        !(text.starts_with("0x") || text.starts_with("0o") || text.starts_with("0b")) && (text.contains('.') || text.contains('e'))
    };
    let channel = |piece: Piece| matches!(s[piece.0..piece.1].replace('_', "").parse::<i64>(), Ok(0) | Ok(1));
    let mut out: Vec<Piece> = Vec::with_capacity(pieces.len());
    let mut buf: Vec<Piece> = vec![];
    let (mut count, mut floats, mut number) = (0, false, true);
    for piece in pieces {
        loop {
            let text = &s[piece.0..piece.1];
            if buf.is_empty() {
                if text == "(" && piece.2 == DEFAULT && open {
                    buf.push(piece);
                    (count, floats, number) = (0, false, true);
                } else {
                    out.push(piece);
                    if !blank(s, piece) && piece.2 != COMMENT { open = unary(s, piece); }
                }
                break;
            }
            if text == " " {
                buf.push(piece);
                break;
            }
            if piece.2 == NUMBER && number && count < 4 && (float(piece) || channel(piece)) {
                buf.push(piece);
                count += 1;
                floats |= float(piece);
                number = false;
                break;
            }
            if text == "," && piece.2 == DEFAULT && !number && count < 4 {
                buf.push(piece);
                number = true;
                break;
            }
            if text == ")" && piece.2 == DEFAULT && !number && (count == 3 || count == 4) && floats {
                out.push((buf[0].0, piece.1, COLOR3));
                open = false;
                buf.clear();
                break;
            }
            for held in buf.drain(..) {
                out.push(held);
                if !blank(s, held) { open = unary(s, held); }
            }
        }
    }
    out.extend(buf);
    out
}

/// The colour runs of `line`, which starts in string state `state` (0: in
/// code; else 1 + 2 * kind + quote, kind 0 a docstring, 1 a prefixed string,
/// 2 an f-string, quote 0 `"""`, 1 `'''`), as (column, characters, kind).
/// `call`: the line starts right inside the parentheses of a call.
pub fn runs(line: &str, state: u8, call: bool) -> Vec<(u32, u32, &'static str)> {
    let mut pieces: Vec<Piece> = vec![];
    if state == 0 {
        raw(line, 0, line.len(), call, &mut pieces);
        pieces = tuples(line, signs(line, pieces, true), true);
    } else {
        // The string the line starts in, then code as after any string.
        let quote = if (state - 1) % 2 == 0 { "\"\"\"" } else { "'''" };
        let cut = line.find(quote).map_or(line.len(), |close| close + 3);
        match (state - 1) / 2 {
            2 => fstring(line, 0, cut, quote, 0, false, &mut pieces),
            kind => icons(line, 0, cut, if kind == 0 { DOC } else { STRING }, &mut pieces),
        }
        let mut rest = vec![];
        raw(line, cut, line.len(), false, &mut rest);
        pieces.extend(tuples(line, signs(line, rest, false), false));
    }
    let mut found: Vec<(u32, u32, &'static str)> = vec![];
    let (mut column, mut last) = (0u32, u8::MAX);
    for (from, to, kind) in pieces {
        let size = if line.is_ascii() { to - from } else { line[from..to].chars().count() } as u32;
        if size == 0 { continue; }
        if kind == last && kind < JOINED {
            found.last_mut().unwrap().1 += size;
        } else {
            found.push((column, size, KINDS[kind as usize]));
        }
        (column, last) = (column + size, kind);
    }
    found
}
