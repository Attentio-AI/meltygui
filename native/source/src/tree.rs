//! The text as a tree of keys and values: what `Source.tree` reads and
//! writes (`meltygui/model/tree_model.py`).
//!
//! A reading's tree is a list of parts, one per top-level statement
//! (`Facts::units`), each a flat table of its entries. A part is grown by
//! the statement reader of the source sites (`Facts::unit`) and is shared
//! between readings: `Tree::grow` takes the part of every statement whose
//! text is where it was from the tree before and grows only the others, so
//! a reading after an edit costs the edited statements plus one step per
//! top-level statement, never the file's tokens. It runs on the reading
//! thread when the request asks for the tree. An entry's keys from the
//! module down are its statement's site key (`Facts::sites`), so a tree
//! path names a breakpoint's statement too:
//!
//! ```text
//! name = value            the value: a leaf, or a branch of its items
//! class Name:             a branch of its body
//! def name(a, b=1):       a branch: `parameters` (a default, or EMPTY) and `locals` (its body)
//! if / for / try ..       a branch of its body (`if##0`, `for x in y`); an `if` holds its test as `##if`
//! name(1, key=2)          a call statement (`name()`): a branch of its arguments, by index and by keyword
//! ```
//!
//! A value is INT, FLOAT, STR, BOOL or NONE when it is that literal, a
//! LIST, TUPLE, DICT, SET or CALL branch of its items, and CODE (its source
//! text) for every other expression. Nothing here is a syntax tree: entries
//! are rows (key, kind, parent, span), asked for by entry and key. An entry
//! is named by a `u64`: its part's index and its row in the part; 0 is the module.
//!
//! Levers: `Grower::simple` and `compound` choose the statements that are
//! entries, `Grower::fill` the literals that are read as values. Not
//! surfaced: imports, `return` and the other keyword statements, targets
//! that are no single name (`self.x = 1`, `a, b = 1, 2`), decorators.
use std::sync::Arc;

use crate::facts::{hash, mix, Facts, Keyed, Word};
use crate::scan;

/// Entry kinds; the names Python shows are `KINDS`. A kind below `INT` has entries of its own.
pub const MODULE: u8 = 0;
pub const CLASS: u8 = 1;
pub const DEF: u8 = 2;
pub const PARAMS: u8 = 3;
pub const LOCALS: u8 = 4;
pub const BLOCK: u8 = 5;
pub const CALL: u8 = 6;
pub const LIST: u8 = 7;
pub const TUPLE: u8 = 8;
pub const DICT: u8 = 9;
pub const SET: u8 = 10;
pub const INT: u8 = 11;
pub const FLOAT: u8 = 12;
pub const STR: u8 = 13;
pub const BOOL: u8 = 14;
pub const NONE: u8 = 15;
pub const CODE: u8 = 16;
/// A parameter without a default value: its span is the empty place one goes.
pub const EMPTY: u8 = 17;
pub const KINDS: [&str; 18] = ["module", "class", "def", "parameters", "locals", "block", "call", "list", "tuple", "dict", "set",
                               "int", "float", "str", "bool", "none", "code", "empty"];

/// Lever: containers nested deeper than this are read as code.
const DEEP: u8 = 32;

#[derive(Clone, PartialEq, Eq, Debug)]
pub enum Key {
    Name(Box<str>),
    Index(i64),
}

impl Key {
    fn hash(&self) -> u64 {
        match self {
            Key::Name(name) => hash(name),
            Key::Index(at) => mix(0x0069_6e64_6578, *at as u64),
        }
    }
}

pub struct Entry {
    pub key: Key,
    pub kind: u8,
    /// Its row in the part of the entry it is in.
    pub parent: u32,
    /// Its place among its parent's entries (in the part: a top-level one's place in the module is `Tree::place`).
    pub at: u32,
    /// Where its text stands, which a write replaces: (line, column, last
    /// line, end column), lines counted from the part's first, columns in
    /// characters, the end exclusive. A branch of statements: its header.
    pub span: (u32, u32, u32, u32),
}

/// What a leaf entry holds, read from its text.
pub enum Leaf {
    Int(i64),
    Float(f64),
    Str(String),
    Bool(bool),
    None,
    Code(String),
    /// A parameter without a default, or an entry that has entries.
    Nothing,
}

/// A counter of the module's body that numbers keys (`Facts::sites`): how
/// often a key was given (`x`, `x#1`), the `if` / `elif` / `else` counts,
/// how often a block's text was (`try`, `try##1`).
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum Counter {
    Seen(String),
    Branch(u8),
    Block(String),
}

impl Counter {
    fn id(&self) -> u64 {
        match self {
            Counter::Seen(key) => mix(1, hash(key)),
            Counter::Branch(at) => mix(2, *at as u64),
            Counter::Block(text) => mix(3, hash(text)),
        }
    }
}

/// The entries of one top-level statement with what belongs to it. Row 0
/// stands for the module: the statement's own entries are its entries.
#[derive(Default)]
pub struct Part {
    pub entries: Vec<Entry>,
    /// The entries of row i, in text order: `kids[first[i] .. first[i + 1]]`.
    kids: Vec<u32>,
    first: Vec<u32>,
    /// (parent, key) hashed onto the row.
    named: Keyed<u32>,
    /// The module's counters its keys were numbered by: (counter, where it
    /// stood before the part, how far the part moved it). The part holds
    /// for another reading while they stand there again.
    pub touched: Vec<(Counter, u32, u32)>,
    /// `touched` with each counter hashed, and what a unit keeps of the part beside it.
    moves: Vec<(u64, u32, u32)>,
    sum: Sum,
}

/// What `Tree::grow` reads of a part for every unit, kept in the unit so a
/// part that is taken again is not looked into: every (parent, key) in
/// order, hashed; the module's keys with their rows, hashed; how many of
/// those and how many entries; the one counter it moved (`moves`: 0 none, 1
/// this one, 2 several, read from the part).
#[derive(Clone, Copy, Default)]
struct Sum {
    shape: u64,
    tops: u64,
    count: u32,
    entries: u32,
    moves: u8,
    moved: (u64, u32, u32),
}

impl Part {
    fn count(&self, row: u32) -> usize {
        self.first.get(row as usize + 1).map_or(0, |&end| (end - self.first[row as usize]) as usize)
    }

    fn child(&self, row: u32, at: usize) -> Option<u32> {
        (at < self.count(row)).then(|| self.kids[self.first[row as usize] as usize + at])
    }

    fn find(&self, row: u32, key: &Key) -> Option<u32> {
        let fits = |&id: &u32| self.entries.get(id as usize).is_some_and(|entry| id != 0 && entry.parent == row && entry.key == *key);
        let found = self.named.get(&mix(row as u64, key.hash())).copied().filter(fits);
        // (Two keys of one hash: the one that came second is looked for among its siblings.)
        found.or_else(|| (0..self.count(row)).filter_map(|at| self.child(row, at)).find(fits))
    }
}

/// One top-level statement of a reading: its part, the key of its text
/// (`Facts::units`: what the next reading takes the part by), the line it
/// starts on, how many entries the module has before it.
struct Unit {
    part: Arc<Part>,
    key: u64,
    line: u32,
    before: u32,
    sum: Sum,
}

/// Lever of `Tree::grow`: how far past where the last part was taken (or
/// past the first unit of its chunk) the tree before is looked through for
/// a unit. No more units than this start in one chunk.
const AHEAD: usize = 2 * crate::store::LINES + 1;

#[derive(Default)]
pub struct Tree {
    units: Vec<Unit>,
    /// The module's keys, hashed, onto their entries: shared with the tree before while `tops` is the same.
    named: Arc<Keyed<u64>>,
    /// Each chunk a unit starts in (`Chunk::id`) onto the first such unit: where the next
    /// reading finds its units again after an edit that removed or moved many.
    chunks: Keyed<u32>,
    /// The module's keys with their units and rows, hashed.
    signed: u64,
    /// The module's entries in all, and the entries of every depth.
    tops: u32,
    pub size: usize,
    /// Every (parent, key) in order, hashed: two readings with the same
    /// one have the same keys everywhere, and differ in values at most.
    pub shape: u64,
    /// Parts taken from the tree before, and parts grown: what a reading cost.
    pub kept: u32,
    pub grown: u32,
}

fn id(unit: usize, row: u32) -> u64 {
    (unit as u64) << 32 | row as u64
}

impl Tree {
    /// The tree of the text of `facts`. The part of a statement whose text
    /// is where it was in `before`, and whose keys are numbered as they
    /// were, is taken from there; the others are grown.
    pub fn grow(facts: &Facts, before: Option<&Tree>) -> Tree {
        let units = facts.units();
        let mut tree = Tree { units: Vec::with_capacity(units.len()), ..Tree::default() };
        // Where the module's counters stand before the unit being placed.
        let mut counters: Keyed<u32> = Keyed::with_capacity_and_hasher(units.len() * 2, Default::default());
        let old: &[Unit] = before.map_or(&[], |before| &before.units);
        // The units of the tree before come in the same order but for an edit: `next` is where the last one was
        // found. One that is not right there is looked for from the first unit of its chunk: an edit left the
        // chunks it did not touch as they were, wherever they are now.
        let (mut next, mut chunk) = (0usize, u64::MAX);
        let ahead = |from: usize, key: u64| old[from.min(old.len())..].iter().take(AHEAD).position(|unit| unit.key == key).map(|at| from + at);
        for (first, last, key, starts) in units {
            if starts != chunk {
                chunk = starts;
                tree.chunks.insert(chunk, tree.units.len() as u32);
            }
            let found = ahead(next, key).or_else(|| before.and_then(|before| before.chunks.get(&starts)).and_then(|&from| ahead(from as usize, key)));
            // Its counters stand where they did: checked while they are moved on (and moved back when one does not).
            let kept = found.filter(|&at| {
                let unit = &old[at];
                let moves: &[(u64, u32, u32)] = match unit.sum.moves { 0 => &[], 1 => std::slice::from_ref(&unit.sum.moved), _ => &unit.part.moves };
                let mut stands = true;
                for (counter, start, moved) in moves {
                    let slot = counters.entry(*counter).or_insert(0);
                    stands &= *slot == *start;
                    *slot += moved;
                }
                if !stands {
                    for (counter, _, moved) in moves { *counters.get_mut(counter).unwrap() -= moved; }
                }
                stands
            });
            let (part, sum) = match kept {
                Some(at) => {
                    tree.kept += 1;
                    next = at + 1;
                    (old[at].part.clone(), old[at].sum)
                }
                None => {
                    tree.grown += 1;
                    // Alone first: that tells which counters it numbers its keys by. Where one of
                    // them does not stand at 0 (a name the module gave before, an `if` above), again from there.
                    let stands = |counter: &Counter| counters.get(&counter.id()).copied().unwrap_or(0);
                    let mut part = facts.unit(first, last, &[]);
                    let seeds: Vec<(Counter, u32)> = part.touched.iter().map(|(counter, ..)| (counter.clone(), stands(counter)))
                        .filter(|seed| seed.1 != 0).collect();
                    if !seeds.is_empty() { part = facts.unit(first, last, &seeds); }
                    for (counter, _, moved) in &part.moves { *counters.entry(*counter).or_insert(0) += moved; }
                    let sum = part.sum;
                    (Arc::new(part), sum)
                }
            };
            tree.shape = mix(tree.shape, sum.shape);
            tree.signed = mix(tree.signed, sum.tops);
            tree.size += sum.entries as usize;
            tree.units.push(Unit { part, key, line: first, before: tree.tops, sum });
            tree.tops += sum.count;
        }
        tree.named = match before.filter(|before| before.signed == tree.signed && before.units.len() == tree.units.len()) {
            // The module's keys are where they were: so is what they are found by.
            Some(before) => before.named.clone(),
            None => Arc::new(tree.units.iter().enumerate().flat_map(|(unit, held)| {
                (0..held.part.count(0)).filter_map(move |at| held.part.child(0, at)).map(move |row| (held.part.entries[row as usize].key.hash(), id(unit, row)))
            }).collect()),
        };
        tree
    }

    fn part(&self, node: u64) -> Option<(&Unit, u32)> {
        self.units.get((node >> 32) as usize).map(|unit| (unit, node as u32)).filter(|(unit, row)| (*row as usize) < unit.part.entries.len())
    }

    /// The entry `node` names; None for the module and for a name of another reading that fits none.
    pub fn entry(&self, node: u64) -> Option<&Entry> {
        self.part(node).filter(|_| node as u32 != 0).map(|(unit, row)| &unit.part.entries[row as usize])
    }

    pub fn count(&self, node: u64) -> usize {
        if node == 0 { return self.tops as usize; }
        self.part(node).map_or(0, |(unit, row)| unit.part.count(row))
    }

    /// The entry at place `at` among those of `node`.
    pub fn child(&self, node: u64, at: usize) -> Option<u64> {
        if node == 0 {
            if at >= self.tops as usize { return None; }
            let unit = self.units.partition_point(|unit| unit.before as usize <= at) - 1;
            let part = &self.units[unit].part;
            return part.child(0, at - self.units[unit].before as usize).map(|row| id(unit, row));
        }
        let (unit, row) = self.part(node)?;
        unit.part.child(row, at).map(|row| node >> 32 << 32 | row as u64)
    }

    /// The entry of `node` under `key`.
    pub fn find(&self, node: u64, key: &Key) -> Option<u64> {
        if node == 0 {
            let fits = |&found: &u64| self.entry(found).is_some_and(|entry| entry.parent == 0 && entry.key == *key);
            let found = self.named.get(&key.hash()).copied().filter(fits);
            return found.or_else(|| (0..self.tops as usize).filter_map(|at| self.child(0, at)).find(fits));
        }
        let (unit, row) = self.part(node)?;
        unit.part.find(row, key).map(|row| node >> 32 << 32 | row as u64)
    }

    /// The place of `node` among its parent's entries.
    pub fn place(&self, node: u64) -> u32 {
        let Some(((unit, _), entry)) = self.part(node).zip(self.entry(node)) else { return 0 };
        entry.at + if entry.parent == 0 { unit.before } else { 0 }
    }

    /// Where the text of `node` stands in the reading: (line, column, last line, end column).
    pub fn span(&self, node: u64) -> Option<(u32, u32, u32, u32)> {
        let ((unit, _), entry) = self.part(node).zip(self.entry(node))?;
        Some((entry.span.0 + unit.line, entry.span.1, entry.span.2 + unit.line, entry.span.3))
    }

    /// What the leaf `node` holds, read from the text of `facts`.
    pub fn leaf(&self, facts: &Facts, node: u64) -> Leaf {
        let Some((entry, (line, column, last, end))) = self.entry(node).zip(self.span(node)) else { return Leaf::Nothing };
        if entry.kind < INT || entry.kind == EMPTY { return Leaf::Nothing; }
        let text = facts.slice(line, column, last, end);
        match entry.kind {
            INT | FLOAT => match signed(&text) {
                Some(Number::Int(found)) => Leaf::Int(found),
                Some(Number::Float(found)) => Leaf::Float(found),
                None => Leaf::Code(text),
            },
            STR => string(&text).map_or_else(|| Leaf::Code(text), Leaf::Str),
            BOOL => Leaf::Bool(text == "True"),
            NONE => Leaf::None,
            _ => Leaf::Code(text),
        }
    }

    /// The source text of `node`.
    pub fn text(&self, facts: &Facts, node: u64) -> Option<String> {
        self.span(node).map(|(line, column, last, end)| facts.slice(line, column, last, end))
    }
}

enum Number {
    Int(i64),
    Float(f64),
}

/// The number a NUMBER token spells; None for what is none Python reads as an int or a float (`1j`, an int past 64 bits).
fn number(text: &str) -> Option<Number> {
    let digits: String = text.chars().filter(|c| *c != '_').collect();
    let lower = digits.to_ascii_lowercase();
    for (prefix, radix) in [("0x", 16), ("0o", 8), ("0b", 2)] {
        if let Some(rest) = lower.strip_prefix(prefix) { return i64::from_str_radix(rest, radix).ok().map(Number::Int); }
    }
    if lower.bytes().all(|byte| byte.is_ascii_digit()) {
        let plain = lower.len() == 1 || !lower.starts_with('0') || lower.bytes().all(|byte| byte == b'0');
        return if plain { lower.parse().ok().map(Number::Int) } else { None };
    }
    let float = lower.starts_with(|c: char| c.is_ascii_digit() || c == '.') && lower.bytes().all(|byte| byte.is_ascii_digit() || b".e+-".contains(&byte));
    if float { lower.parse().ok().map(Number::Float) } else { None }
}

/// A number with the sign before it.
fn signed(text: &str) -> Option<Number> {
    let (minus, digits) = match text.as_bytes().first() {
        Some(b'-') => (true, text[1..].trim_start()),
        Some(b'+') => (false, text[1..].trim_start()),
        _ => (false, text),
    };
    Some(match number(digits)? {
        Number::Int(found) => Number::Int(if minus { -found } else { found }),
        Number::Float(found) => Number::Float(if minus { -found } else { found }),
    })
}

/// The text a string token spells: one literal on one line, plain or raw.
/// None for bytes, f-strings and what the escapes here do not cover (`\N{..}`).
fn string(text: &str) -> Option<String> {
    let body = text.trim_start_matches(|c| "rRuU".contains(c));
    let prefix = &text[..text.len() - body.len()];
    let quote = body.chars().next().filter(|c| *c == '"' || *c == '\'')?;
    if prefix.len() > 2 { return None; }
    let size = if body.len() >= 6 && body[1..].starts_with(quote) && body[2..].starts_with(quote) { 3 } else { 1 };
    if body.len() < size * 2 || !body.ends_with(&body[..size]) { return None; }
    let inner = &body[size..body.len() - size];
    if prefix.contains(['r', 'R']) { return Some(inner.to_string()); }
    let mut found = String::with_capacity(inner.len());
    let mut chars = inner.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '\\' {
            found.push(c);
            continue;
        }
        let hex = |count: usize, chars: &mut std::iter::Peekable<std::str::Chars>| {
            let digits: String = chars.take(count).collect();
            (digits.len() == count).then(|| u32::from_str_radix(&digits, 16).ok()).flatten().and_then(char::from_u32)
        };
        match chars.next()? {
            'n' => found.push('\n'),
            't' => found.push('\t'),
            'r' => found.push('\r'),
            'a' => found.push('\x07'),
            'b' => found.push('\x08'),
            'f' => found.push('\x0c'),
            'v' => found.push('\x0b'),
            'x' => found.push(hex(2, &mut chars)?),
            'u' => found.push(hex(4, &mut chars)?),
            'U' => found.push(hex(8, &mut chars)?),
            'N' => return None,
            digit @ '0'..='7' => {
                let mut code = digit as u32 - '0' as u32;
                for _ in 0..2 {
                    match chars.peek().and_then(|next| next.to_digit(8)) {
                        Some(next) => { code = code * 8 + next; chars.next(); }
                        None => break,
                    }
                }
                found.push(char::from_u32(code)?);
            }
            same @ ('\\' | '\'' | '"') => found.push(same),
            other => { found.push('\\'); found.push(other); }
        }
    }
    Some(found)
}

fn op(word: &Word, text: &str) -> bool {
    word.3 == scan::OP && word.4 == text
}

/// Where the bracket `words[0]` opens closes.
fn close(words: &[Word]) -> Option<usize> {
    let mut depth = 0;
    words.iter().position(|word| {
        depth += match (word.3, word.4) {
            (scan::OP, "(" | "[" | "{") => 1,
            (scan::OP, ")" | "]" | "}") => -1,
            _ => 0,
        };
        depth == 0
    })
}

/// The items between a pair of brackets, parted by commas outside inner
/// brackets (a trailing comma parts nothing); None when they are no list
/// of plain items: a comprehension, a lambda (its commas are its own), an empty item.
fn items<'w, 'a>(inner: &'w [Word<'a>]) -> Option<Vec<&'w [Word<'a>]>> {
    let (mut depth, mut from, mut found) = (0, 0, vec![]);
    for (at, word) in inner.iter().enumerate() {
        match (word.3, word.4) {
            (scan::OP, "(" | "[" | "{") => depth += 1,
            (scan::OP, ")" | "]" | "}") => depth -= 1,
            (scan::KEYWORD, "for" | "lambda" | "async") if depth == 0 => return None,
            (scan::OP, ",") if depth == 0 => {
                if at == from { return None; }
                found.push(&inner[from..at]);
                from = at + 1;
            }
            _ => {}
        }
    }
    if from < inner.len() { found.push(&inner[from..]); }
    Some(found)
}

/// The key a dict literal's key spells: a plain string or an int.
fn literal(words: &[Word]) -> Option<Key> {
    match words {
        [word] if word.3 == scan::STRING => string(word.4).map(|text| Key::Name(text.into())),
        [word] if word.3 == scan::NUMBER => match number(word.4)? {
            Number::Int(found) => Some(Key::Index(found)),
            Number::Float(_) => None,
        },
        _ => None,
    }
}

/// A part being grown, by the statement reader (`Facts::unit`).
pub struct Grower {
    tree: Part,
    /// How many entries each entry has so far.
    counts: Vec<u32>,
    /// The line the part starts on: its spans count from there.
    line: u32,
}

impl Grower {
    pub fn new(line: u32) -> Grower {
        let mut grower = Grower { tree: Part::default(), counts: vec![], line };
        grower.tree.entries.push(Entry { key: Key::Name("".into()), kind: MODULE, parent: 0, at: 0, span: (0, 0, 0, 0) });
        grower.counts.push(0);
        grower
    }

    /// The finished part: the entries of each entry gathered, in text order.
    pub fn done(mut self, touched: Vec<(Counter, u32, u32)>) -> Part {
        self.tree.moves = touched.iter().map(|(counter, start, moved)| (counter.id(), *start, *moved)).collect();
        self.tree.touched = touched;
        let tree = &mut self.tree;
        tree.first = Vec::with_capacity(self.counts.len() + 1);
        let mut total = 0;
        for count in &self.counts {
            tree.first.push(total);
            total += count;
        }
        tree.first.push(total);
        tree.kids = vec![0; total as usize];
        let mut sum = Sum { entries: tree.entries.len() as u32 - 1, count: self.counts[0], moves: tree.moves.len().min(2) as u8,
                            moved: tree.moves.first().copied().unwrap_or_default(), ..Sum::default() };
        for (id, entry) in tree.entries.iter().enumerate().skip(1) {
            tree.kids[(tree.first[entry.parent as usize] + entry.at) as usize] = id as u32;
            sum.shape = mix(sum.shape, mix(entry.parent as u64, entry.key.hash()));
            if entry.parent == 0 { sum.tops = mix(sum.tops, mix(id as u64, entry.key.hash())); }
        }
        // (A part without keys still counts: the units after it are named by their place.)
        sum.tops = mix(sum.tops, 1);
        tree.sum = sum;
        self.tree
    }

    /// A new entry of `parent`; `span` in lines of the reading.
    fn add(&mut self, parent: u32, key: Key, kind: u8, span: (u32, u32, u32, u32)) -> u32 {
        let id = self.tree.entries.len() as u32;
        let span = (span.0.saturating_sub(self.line), span.1, span.2.saturating_sub(self.line), span.3);
        self.tree.named.entry(mix(parent as u64, key.hash())).or_insert(id);
        self.tree.entries.push(Entry { key, kind, parent, at: self.counts[parent as usize], span });
        self.counts[parent as usize] += 1;
        self.counts.push(0);
        id
    }

    /// The entry `path` names, from the module down. A part nothing made yet is a branch of what follows.
    fn at(&mut self, path: &[String]) -> u32 {
        let mut node = 0;
        for part in path {
            let key = Key::Name(part.as_str().into());
            let found = self.tree.named.get(&mix(node as u64, key.hash())).copied()
                .filter(|&id| id != node && self.tree.entries[id as usize].parent == node && self.tree.entries[id as usize].key == key);
            node = match found {
                Some(id) => id,
                None => self.add(node, key, BLOCK, (self.line, 0, self.line, 0)),
            };
        }
        node
    }

    /// A compound statement's header (`words`, up to its `:`), keyed `path`: `head` is its keyword.
    pub fn compound(&mut self, path: &[String], head: &str, words: &[Word]) {
        let (Some(key), Some(first), Some(last)) = (path.last(), words.first(), words.last()) else { return };
        let parent = self.at(&path[..path.len() - 1]);
        let kind = match head { "class" => CLASS, "def" => DEF, _ => BLOCK };
        let id = self.add(parent, Key::Name(key.as_str().into()), kind, (first.0, first.1, last.0, last.2));
        let from = words.iter().position(|word| word.4 == head).map_or(words.len(), |at| at + 1);
        match head {
            "def" => {
                let params = self.add(id, Key::Name("parameters".into()), PARAMS, (first.0, first.1, last.0, last.2));
                self.parameters(params, &words[from..]);
                self.add(id, Key::Name("locals".into()), LOCALS, (first.0, first.1, last.0, last.2));
            }
            // An `if` holds its test: `##if`, `##elif` (no statement in its body is keyed so).
            "if" | "elif" if from < words.len() => {
                let test = &words[from..];
                self.add(id, Key::Name(format!("##{head}").into()), CODE, (test[0].0, test[0].1, last.0, last.2));
            }
            _ => {}
        }
    }

    /// The parameters of a `def`, from the words after the keyword: each
    /// with its default value, or EMPTY where one would go. Not `self`,
    /// `cls`, `*args`, `**kwargs`.
    fn parameters(&mut self, params: u32, words: &[Word]) {
        let Some(open) = words.iter().position(|word| op(word, "(")) else { return };
        let Some(shut) = close(&words[open..]) else { return };
        let Some(found) = items(&words[open + 1..open + shut]) else { return };
        for item in found {
            let name = item[0];
            if name.3 != scan::NAME || matches!(name.4, "self" | "cls") { continue; }
            let mut depth = 0;
            // The `=` of its default, outside the brackets of its annotation.
            let equals = item.iter().position(|word| {
                depth += match (word.3, word.4) {
                    (scan::OP, "(" | "[" | "{") => 1,
                    (scan::OP, ")" | "]" | "}") => -1,
                    _ => 0,
                };
                depth == 0 && op(word, "=")
            });
            match equals.filter(|&equals| equals + 1 < item.len()) {
                Some(equals) => { self.value(params, Key::Name(name.4.into()), &item[equals + 1..], 0); }
                None => {
                    let end = item[item.len() - 1];
                    self.add(params, Key::Name(name.4.into()), EMPTY, (end.0, end.2, end.0, end.2));
                }
            }
        }
    }

    /// A simple statement keyed `path` whose `words` are a value: what a name is assigned, or a call.
    pub fn simple(&mut self, path: &[String], words: &[Word]) {
        let Some(key) = path.last().filter(|_| !words.is_empty()) else { return };
        let parent = self.at(&path[..path.len() - 1]);
        self.value(parent, Key::Name(key.as_str().into()), words, 0);
    }

    /// The entry of the expression `words` under `key` of `parent`, with the entries of its items.
    fn value(&mut self, parent: u32, key: Key, words: &[Word], depth: u8) -> u32 {
        let (first, last) = (words[0], words[words.len() - 1]);
        let id = self.add(parent, key, CODE, (first.0, first.1, last.0, last.2));
        self.tree.entries[id as usize].kind = self.fill(id, words, depth);
        id
    }

    /// The kind of the expression `words`, which is entry `id`; a container's items become its entries.
    fn fill(&mut self, id: u32, words: &[Word], depth: u8) -> u8 {
        match words {
            [word] => return match (word.3, word.4) {
                (scan::NUMBER, text) => match number(text) { Some(Number::Int(_)) => INT, Some(Number::Float(_)) => FLOAT, None => CODE },
                (scan::STRING, text) => if string(text).is_some() { STR } else { CODE },
                (scan::KEYWORD, "True" | "False") => BOOL,
                (scan::KEYWORD, "None") => NONE,
                _ => CODE,
            },
            [sign, word] if (op(sign, "-") || op(sign, "+")) && word.3 == scan::NUMBER => {
                return match number(word.4) { Some(Number::Int(_)) => INT, Some(Number::Float(_)) => FLOAT, None => CODE };
            }
            _ => {}
        }
        if depth >= DEEP { return CODE; }
        // A call of a dotted name: `name(..)`, `a.b(..)`.
        let open = words.iter().position(|word| word.3 == scan::OP && matches!(word.4, "(" | "[" | "{")).unwrap_or(words.len());
        if open >= words.len() || close(&words[open..]) != Some(words.len() - 1 - open) { return CODE; }
        let Some(found) = items(&words[open + 1..words.len() - 1]) else { return CODE };
        let dotted = words[..open].iter().enumerate().all(|(at, word)| if at % 2 == 0 { word.3 == scan::NAME } else { op(word, ".") });
        match (open, words[open].4) {
            (0, "[") => {
                for (at, item) in found.iter().enumerate() { self.value(id, Key::Index(at as i64), item, depth + 1); }
                LIST
            }
            // Brackets around one expression: the entry is what they hold.
            (0, "(") if found.len() == 1 && !op(&words[words.len() - 2], ",") => {
                let inner = found[0];
                self.tree.entries[id as usize].span = (inner[0].0 - self.line, inner[0].1, inner[inner.len() - 1].0 - self.line, inner[inner.len() - 1].2);
                self.fill(id, inner, depth + 1)
            }
            (0, "(") => {
                for (at, item) in found.iter().enumerate() { self.value(id, Key::Index(at as i64), item, depth + 1); }
                TUPLE
            }
            (0, "{") => {
                // `key: value` in every item and a literal for every key, each once: a dict. In none: a set.
                let colons: Vec<Option<usize>> = found.iter().map(|item| {
                    let mut depth = 0;
                    item.iter().position(|word| {
                        depth += match (word.3, word.4) {
                            (scan::OP, "(" | "[" | "{") => 1,
                            (scan::OP, ")" | "]" | "}") => -1,
                            _ => 0,
                        };
                        depth == 0 && op(word, ":")
                    })
                }).collect();
                if colons.iter().all(Option::is_none) && !found.is_empty() {
                    for (at, item) in found.iter().enumerate() { self.value(id, Key::Index(at as i64), item, depth + 1); }
                    return SET;
                }
                let mut keys: Vec<Key> = Vec::with_capacity(found.len());
                for (item, colon) in found.iter().zip(&colons) {
                    let Some(colon) = colon.filter(|&colon| colon + 1 < item.len()) else { return CODE };
                    match literal(&item[..colon]) {
                        Some(key) if !keys.contains(&key) => keys.push(key),
                        _ => return CODE,
                    }
                }
                for ((item, colon), key) in found.iter().zip(&colons).zip(keys) {
                    self.value(id, key, &item[colon.unwrap() + 1..], depth + 1);
                }
                DICT
            }
            (_, "(") if open % 2 == 1 && dotted => {
                let mut at = 0;
                for item in found {
                    if item.len() > 2 && item[0].3 == scan::NAME && op(&item[1], "=") {
                        self.value(id, Key::Name(item[0].4.into()), &item[2..], depth + 1);
                    } else {
                        self.value(id, Key::Index(at), item, depth + 1);
                        at += 1;
                    }
                }
                CALL
            }
            _ => CODE,
        }
    }
}
