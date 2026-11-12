//! Facts about a whole text, read off the chunk scans in one pass over the
//! lines: fold ranges, definitions, bindings by scope, search hits.
//!
//! The pass compares indents and flags and hashes names. It allocates
//! nothing per line or per fold, so a reading after an edit costs a fraction
//! of the chunk scans it reuses (`scan.rs` caches those on the chunks). A
//! fold's name is kept as its `key`, a hash; `Facts::name` spells it out
//! where one is shown or saved. To add a fact: collect it in `Facts::read`,
//! give it a field.
use std::cell::OnceCell;
use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasherDefault, Hasher};
use std::sync::Arc;

use crate::bind::{self, Binder, Binding, Bound};
use crate::call::{self, Args, Head, Param};
use crate::scan::{self, strip, Tok, KEYWORDS};
use crate::store::Chunk;

/// Fold kinds, in the order of `KINDS`. A scope or block has an indent guide.
pub const SCOPE: u8 = 0;
pub const BLOCK: u8 = 1;
pub const STR: u8 = 2;
pub const COMMENT: u8 = 3;
pub const IMPORTS: u8 = 4;
const KINDS: [&str; 5] = ["scope", "block", "str", "comment", "imports"];

/// A fold's name: what it is by content, so it survives line shifts.
/// `("scope", path...)`, `("block", path..., keyword, header)`,
/// `("str", first line)`, `("comment", first line)`, `("imports",)`; `twin`
/// counts earlier folds of the same name.
#[derive(Clone, PartialEq, Eq)]
pub struct Name {
    pub parts: Vec<Box<str>>,
    pub twin: u32,
}

impl Name {
    /// The hash `Facts::read` gives a fold of this name.
    pub fn key(&self) -> u64 {
        twinned(self.parts.iter().fold(SEED, |key, part| mix(key, hash(part))), self.twin)
    }
}

const SEED: u64 = 0x6d65_6c74_7900_0001;

/// The names every module sees without binding them (builtins and the
/// module's own dunders): given by Python once (`known`).
pub static KNOWN: std::sync::OnceLock<HashSet<String>> = std::sync::OnceLock::new();

pub fn hash(text: &str) -> u64 {
    text.bytes().fold(0xcbf2_9ce4_8422_2325, |hash, byte| (hash ^ byte as u64).wrapping_mul(0x100_0000_01b3))
}

pub(crate) fn mix(key: u64, part: u64) -> u64 {
    let mixed = (key.rotate_left(23) ^ part).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    mixed ^ mixed >> 29
}

fn twinned(key: u64, twin: u32) -> u64 {
    if twin > 0 { mix(key, !(twin as u64)) } else { key }
}

/// Hasher for keys that are hashes already.
#[derive(Default)]
pub struct Plain(u64);

impl Hasher for Plain {
    fn finish(&self) -> u64 { self.0 }
    fn write(&mut self, _: &[u8]) { unreachable!() }
    fn write_u64(&mut self, key: u64) { self.0 = key; }
}

pub type Keyed<T> = HashMap<u64, T, BuildHasherDefault<Plain>>;

pub struct Def {
    pub class: bool,
    pub line: u32,
    pub last: u32,
    pub indent: u32,
    /// Index of the definition around this one, or -1.
    pub parent: i32,
}

#[derive(Default)]
pub struct Facts {
    pub version: u64,
    /// The text the facts were read from: names are spelled from it.
    chunks: Vec<Arc<Chunk>>,
    /// Lines before chunk i.
    line0: Vec<u32>,
    // Fold ranges, outermost first, in columns (`rows.rs` keeps a session's copy on its lines).
    pub heads: Vec<u32>,
    pub spans: Vec<u32>,
    pub parents: Vec<i32>,
    /// Each range's name, hashed: what collapsed state is kept by.
    pub keys: Vec<u64>,
    pub kinds: Vec<u8>,
    /// Indent in spaces of the range's first line.
    pub indents: Vec<u32>,
    twins: Vec<u32>,
    /// The definition a scope is, or a block sits in (-1: none).
    owners: Vec<i32>,
    /// The ranges that start collapsed.
    pub defaults: Vec<u32>,
    /// (line, twin) of the lone metadata comments: folds by name, without a range.
    metas: Vec<(u32, u32)>,
    pub defs: Vec<Def>,
    /// What `hits` are the occurrences of.
    pub term: String,
    /// The string state each chunk is entered in.
    entries: Vec<u8>,
    /// Every binding, in line order.
    pub binds: Vec<Binding>,
    /// Indexes into `binds` of each scope's names, once each: those of scope
    /// s (-1: the module) are `names[first[s + 1] .. first[s + 2]]`.
    names: Vec<u32>,
    first: Vec<u32>,
    /// What the chunks bind that were read with a statement open above them
    /// (by chunk index): the others keep theirs (`Chunk::bound`).
    live: Vec<(u32, Bound)>,
    /// Every (name, scope) bound, hashed, onto the index in `binds` of its
    /// one binding (`MANY`: it is bound more than once): what a name lookup asks.
    bound: Keyed<u32>,
    pub star: bool,
    /// Lines continuing a statement that the legacy fold scanner read as
    /// statements of their own (it ended or opened a scope there).
    pub bent: u32,
    /// What a chunk's lint findings hold for, beyond the chunk itself: every
    /// binding and every header hashed (`print`), and per chunk the
    /// definitions open where it starts (`opens`). See `Facts::lint`.
    print: u64,
    opens: Vec<u64>,
    /// What cannot be Python, as far as the reader tells for sure: (line,
    /// CPython's message) for a bracket never closed, closed without being
    /// open or by another kind, and for a triple-quoted string still open
    /// where the text ends.
    pub flaws: Vec<(u32, String)>,
    /// The statement table by line (`sited`), made once when a reading is
    /// asked for with site keys: what breakpoints are found by.
    sited: std::sync::OnceLock<(Vec<Site>, Vec<u32>)>,
    /// The text as a tree of keys and values (`tree.rs`), made once when a
    /// reading is asked for with it: what `Source.tree` reads.
    grown: std::sync::OnceLock<Arc<crate::tree::Tree>>,
}

/// One source site: (line, column, last line, end column, kind, key path).
pub type Site = (u32, u32, u32, u32, &'static str, Vec<String>);

/// What the lint finds in one chunk, kept on the chunk (`Chunk::lint`)
/// and taken again by the next reading while `key` is the same.
pub struct Lint {
    key: u64,
    /// The chunks before and after this one that the findings also hang on:
    /// those a statement open at its start began in, those a call closes in.
    back: u32,
    ahead: u32,
    /// The names used that nothing binds: (chunk line, byte, bytes, it
    /// stands in an annotation), `try` guards not looked at yet.
    names: Vec<(u32, u32, u16, bool)>,
    /// The misfitting calls: (chunk line, byte of the chain, message, it is
    /// judged by a signature of the running process: a `try` may guard it).
    calls: Vec<(u32, u32, String, bool)>,
    /// The chains called that have no signature yet.
    wanted: Vec<String>,
}

/// What `Facts::lint` returns: the undefined names, the misfitting calls,
/// the chains to look up, and how many chunks were read for it.
pub type Linted = (Vec<(u32, u32, String, bool, bool)>, Vec<(u32, u32, String)>, Vec<String>, u32);

/// Levers of `Facts::names`: lines around the caret whose names are
/// offered, bindings looked through per scope, candidates returned.
const NEAR: u32 = 200;
/// Lever: the most undefined names one reading reports.
const MOST: usize = 500;
/// Levers: lines a call's arguments or a function's parameters are read
/// over before giving up; lines a statement is followed over.
const REACH: u32 = 400;
const LONG: u32 = 8192;
const LOOK: usize = 4096;
const OFFER: usize = 128;

/// Lever: the chunks around one that its kept lint findings may hang on (`Facts::lint`).
const AROUND: u32 = 32;

/// In `Facts::bound`: the name is bound more than once in its scope; and
/// then only by imports.
const MANY: u32 = u32::MAX;
const BROUGHT: u32 = u32::MAX - 1;

/// The signatures of what calls reach in the running process, by dotted
/// chain (`builtins.name` for a name nothing binds), as plain rows: (name,
/// 0 positional only / 1 positional or keyword / 2 keyword only / 3 `*` /
/// 4 `**`, it has no default, the type it is annotated with or ""). None:
/// looked up, and not to be judged.
pub type Specs = HashMap<String, Option<Vec<(String, u8, bool, String)>>>;

/// What a call calls: a definition (by index; without its first parameter,
/// a method's `self`), or a class that takes no arguments.
enum Callee {
    Def(usize, bool),
    Nothing,
}

/// A word of a statement: (line, column, end column, token kind, text).
pub(crate) type Word<'a> = (u32, u32, u32, u8, &'a str);

/// The counters a statement's key takes its number from, shared by an
/// `if` with its branches and what they hold: `if` / `elif` / `else`
/// counts, and how often a `for`, `try`, `except` .. key was given.
#[derive(Default)]
struct Counts {
    branches: [u32; 3],
    blocks: HashMap<String, u32>,
}

/// A body being read (`Sites`): the column its header starts at, its key
/// path, how often each key was given in it, its counters, and the last
/// compound statement in it (0 none, 1 if, 2 for / while with its key, 3
/// try): what an `else` belongs to.
#[derive(Default)]
struct Body {
    indent: i64,
    path: Vec<String>,
    seen: HashMap<String, u32>,
    counts: std::rc::Rc<std::cell::RefCell<Counts>>,
    last: (u8, String),
}

/// The statement table being built (`Facts::sites`).
struct Sites<'a> {
    facts: &'a Facts,
    found: Vec<(u32, u32, u32, u32, &'static str, Vec<String>)>,
    open: Vec<Body>,
    /// The decorators read since the last definition: (their site, their key).
    decorators: Vec<(usize, String)>,
    /// The tree grown from the same statements (`Facts::grown`), when one is asked for.
    tree: Option<crate::tree::Grower>,
}

/// `base`, then `base#1`, `base#2` .. (`mark` "#"), as often as it was given before.
fn numbered(seen: &mut HashMap<String, u32>, base: String, mark: &str) -> String {
    let count = seen.entry(base.clone()).or_insert(0);
    *count += 1;
    if *count == 1 { base } else { format!("{base}{mark}{}", *count - 1) }
}

impl<'a> Sites<'a> {
    fn text(&self, words: &[Word]) -> String {
        match (words.first(), words.last()) {
            (Some(first), Some(last)) => self.facts.slice(first.0, first.1, last.0, last.2),
            _ => String::new(),
        }
    }

    /// `words` without the brackets that only group them: `(a or b)`, not `(a, b)` or `(a for a in b)`.
    fn bare<'w, 'b>(mut words: &'w [Word<'b>]) -> &'w [Word<'b>] {
        while words.len() >= 2 && words[0].4 == "(" && words[0].3 == scan::OP {
            let mut depth = 0;
            let closes = words.iter().position(|word| {
                depth += match (word.3, word.4) {
                    (scan::OP, "(" | "[" | "{") => 1,
                    (scan::OP, ")" | "]" | "}") => -1,
                    _ => 0,
                };
                depth == 0
            });
            let mut depth = 0;
            let plain = words[1..words.len() - 1].iter().all(|word| {
                let outside = depth == 0;
                depth += match (word.3, word.4) {
                    (scan::OP, "(" | "[" | "{") => 1,
                    (scan::OP, ")" | "]" | "}") => -1,
                    _ => 0,
                };
                !(outside && ((word.3 == scan::OP && word.4 == ",") || (word.3 == scan::KEYWORD && word.4 == "for")))
            });
            if closes != Some(words.len() - 1) || !plain || words.len() == 2 { break; }
            words = &words[1..words.len() - 1];
        }
        words
    }

    /// The name a call is keyed by when `words` are one call: the dotted
    /// name called or the last name of another chain; Some(None) for a
    /// call of anything else.
    fn call(words: &[Word]) -> Option<Option<String>> {
        let words = Sites::bare(words);
        words.last().filter(|word| word.3 == scan::OP && word.4 == ")")?;
        let (mut depth, mut open) = (0, 0);
        for (at, word) in words.iter().enumerate() {
            match (word.3, word.4) {
                (scan::OP, "(" | "[" | "{") => {
                    if depth == 0 { open = at; }
                    depth += 1;
                }
                (scan::OP, ")" | "]" | "}") => depth -= 1,
                // Outside brackets only names, dots and literals: one primary with its trailers.
                (scan::NAME | scan::STRING | scan::NUMBER, _) | (scan::OP, ".") => {}
                _ if depth == 0 => return None,
                _ => {}
            }
        }
        let called = &words[..open];
        if called.is_empty() || words[open].4 != "(" { return None; }
        let dotted = called.iter().enumerate().all(|(at, word)| if at % 2 == 0 { word.3 == scan::NAME } else { word.4 == "." });
        Some(if dotted && called.len() % 2 == 1 {
            Some(called.iter().map(|word| word.4).collect())
        } else if called.len() >= 2 && called[called.len() - 2].4 == "." && called[called.len() - 1].3 == scan::NAME {
            Some(called[called.len() - 1].4.to_string())
        } else {
            None
        })
    }

    /// Read one statement's words: its sites, with the bodies it opens.
    fn statement(&mut self, words: &[Word]) {
        let Some(&first) = words.first() else { return };
        let last = words[words.len() - 1];
        while self.open.len() > 1 && self.open.last().is_some_and(|open| open.indent >= first.1 as i64) { self.open.pop(); }
        if first.4 == "@" {
            let key = Sites::call(&words[1..]).flatten().unwrap_or_else(|| self.text(&words[1..]));
            self.decorators.push((self.found.len(), key));
            return self.found.push((first.0, first.1 + 1, last.0, last.2, "decorator", vec![]));
        }
        let is_async = first.3 == scan::KEYWORD && first.4 == "async" && words.len() > 1;
        let head = words[is_async as usize];
        let kind = match (head.3, head.4) {
            (scan::KEYWORD, "if" | "elif") => "If",
            (scan::KEYWORD, "else") => "else",
            (scan::KEYWORD, "for") => if is_async { "AsyncFor" } else { "For" },
            (scan::KEYWORD, "try") => "Try",
            (scan::KEYWORD, "except") => "except",
            (scan::KEYWORD, "finally") => "finally",
            (scan::KEYWORD, "while" | "with") => "Opaque",
            (scan::KEYWORD, "def") => if is_async { "AsyncFunctionDef" } else { "FunctionDef" },
            (scan::KEYWORD, "class") => "ClassDef",
            (scan::NAME, "match") if last.4 == ":" && words.len() > 2 && !matches!(words[1].4, "=" | "." | "," | ":") => "Opaque",
            _ => "",
        };
        if kind.is_empty() {
            self.open.last_mut().unwrap().last = (0, String::new());
            return self.simples(words);
        }
        // The header's `:`, outside brackets (not a lambda's).
        let (mut depth, mut lambdas, mut colon) = (0, 0, None);
        for (at, word) in words.iter().enumerate() {
            match (word.3, word.4) {
                (scan::OP, "(" | "[" | "{") => depth += 1,
                (scan::OP, ")" | "]" | "}") => depth -= 1,
                (scan::KEYWORD, "lambda") if depth == 0 => lambdas += 1,
                (scan::OP, ":") if depth == 0 && lambdas > 0 => lambdas -= 1,
                (scan::OP, ":") if depth == 0 => {
                    colon = Some(at);
                    break;
                }
                _ => {}
            }
        }
        let header = &words[..colon.unwrap_or(words.len())];
        let end = colon.map_or(last, |colon| words[colon]);
        let around = self.open.last_mut().unwrap();
        // The key, whether the body shares the counters (an `if` and its branches), and what an `else` after it belongs to.
        let (key, shared, then) = match head.4 {
            "if" | "elif" | "else" if head.4 != "else" || around.last.0 == 1 => {
                let at = ["if", "elif", "else"].iter().position(|word| *word == head.4).unwrap();
                let mut counts = around.counts.borrow_mut();
                counts.branches[at] += 1;
                (format!("{}##{}", head.4, counts.branches[at] - 1), true, (1, String::new()))
            }
            "else" if around.last.0 == 3 => (numbered(&mut around.counts.borrow_mut().blocks, "try else".into(), "##"), false, (0, String::new())),
            "else" => (format!("{} else", around.last.1), false, (0, String::new())),
            "for" => {
                let at = header.iter().position(|word| word.3 == scan::KEYWORD && word.4 == "in").unwrap_or(header.len());
                let text = format!("for {} in {}", self.text(Sites::bare(&header[is_async as usize + 1..at])), self.text(Sites::bare(header.get(at + 1..).unwrap_or(&[]))));
                let around = self.open.last_mut().unwrap();
                let key = numbered(&mut around.counts.borrow_mut().blocks, text, "##");
                (key.clone(), false, (2, key))
            }
            "try" => (numbered(&mut around.counts.borrow_mut().blocks, "try".into(), "##"), false, (3, String::new())),
            "except" => {
                let star = header.get(1).is_some_and(|word| word.4 == "*") as usize;
                let named = header.iter().position(|word| word.3 == scan::KEYWORD && word.4 == "as");
                let caught = &header[(1 + star).min(header.len())..named.unwrap_or(header.len())];
                let mut text = if star == 1 { "except*".to_string() } else { "except".to_string() };
                if !caught.is_empty() { text = format!("{text} {}", self.text(caught)); }
                if let Some(name) = named.and_then(|named| header.get(named + 1)) { text = format!("{text} as {}", name.4); }
                let around = self.open.last_mut().unwrap();
                (numbered(&mut around.counts.borrow_mut().blocks, text, "##"), false, (3, String::new()))
            }
            "finally" => (numbered(&mut around.counts.borrow_mut().blocks, "finally".into(), "##"), false, (0, String::new())),
            "def" | "class" => {
                let name = words.get(is_async as usize + 1).map_or("", |word| word.4);
                (numbered(&mut around.seen, name.to_string(), "#"), false, (0, String::new()))
            }
            _ => {
                let key = numbered(&mut around.seen, format!("##{}", first.4), "#");
                (key.clone(), false, if head.4 == "while" { (2, key) } else { (0, String::new()) })
            }
        };
        let around = self.open.last_mut().unwrap();
        around.last = then;
        let mut path = around.path.clone();
        path.push(key);
        let counts = if shared { around.counts.clone() } else { Default::default() };
        self.found.push((first.0, first.1, end.0, end.2, kind, path.clone()));
        if let Some(tree) = self.tree.as_mut() { tree.compound(&path, head.4, header); }
        if matches!(head.4, "def" | "class") {
            // The decorators above belong to it, numbered by their own names.
            let mut seen = HashMap::new();
            for (site, key) in std::mem::take(&mut self.decorators) {
                let mut named = path.clone();
                named.extend(["decorators".to_string(), numbered(&mut seen, key, "#")]);
                self.found[site].5 = named;
            }
            if head.4 == "def" { path.push("locals".to_string()); }
        }
        self.open.push(Body { indent: first.1 as i64, path, seen: HashMap::new(), counts, last: (0, String::new()) });
        if let Some(colon) = colon.filter(|colon| colon + 1 < words.len()) {
            // Its body on the same line: nothing below belongs to it.
            self.simples(&words[colon + 1..]);
            self.open.pop();
        }
    }

    /// The simple statements among `words`, parted by `;`.
    fn simples(&mut self, words: &[Word]) {
        let (mut depth, mut from) = (0, 0);
        for at in 0..=words.len() {
            if let Some(word) = words.get(at) {
                match (word.3, word.4) {
                    (scan::OP, "(" | "[" | "{") => depth += 1,
                    (scan::OP, ")" | "]" | "}") => depth -= 1,
                    _ => {}
                }
                if !(depth == 0 && word.3 == scan::OP && word.4 == ";") { continue; }
            }
            if at > from { self.simple(&words[from..at]); }
            from = at + 1;
        }
    }

    /// The site of a simple statement, keyed by the name it assigns, the
    /// call it is, or the word it starts with (`##return`, `##statement`).
    fn simple(&mut self, words: &[Word]) {
        let (first, last) = (words[0], words[words.len() - 1]);
        let mut depth = 0;
        // The `=` and the first `:` outside brackets (a lambda's `:` aside).
        let (mut equals, mut colon, mut lambdas) = (vec![], None, 0);
        for (at, word) in words.iter().enumerate() {
            match (word.3, word.4) {
                (scan::OP, "(" | "[" | "{") => depth += 1,
                (scan::OP, ")" | "]" | "}") => depth -= 1,
                (scan::KEYWORD, "lambda") if depth == 0 => lambdas += 1,
                (scan::OP, ":") if depth == 0 && lambdas > 0 => lambdas -= 1,
                (scan::OP, ":") if depth == 0 => { colon.get_or_insert(at); }
                // (Not the `=` of a lambda's default values.)
                (scan::OP, "=") if depth == 0 && lambdas == 0 => equals.push(at),
                _ => {}
            }
        }
        let noted = colon.is_some_and(|colon| equals.first().map_or(true, |&equals| colon < equals));
        let kind = match (first.3, first.4) {
            (scan::KEYWORD, "import") => "Import",
            (scan::KEYWORD, "from") => "ImportFrom",
            (scan::KEYWORD, "return" | "raise" | "assert" | "pass" | "break" | "continue" | "del" | "global" | "nonlocal" | "yield") => "Opaque",
            (scan::NAME, "case") if colon.is_some() && words.len() > 1 && !matches!(words[1].4, "=" | ":" | ".") => "Expr",
            _ if noted => "AnnAssign",
            _ if !equals.is_empty() => "Assign",
            _ => "Expr",
        };
        // One name assigned a value: `name = ..`, `name: type = ..`.
        let name = (first.3 == scan::NAME && match kind {
            "Assign" => equals == [1],
            "AnnAssign" => colon == Some(1) && !equals.is_empty(),
            _ => false,
        }).then(|| first.4.to_string());
        let call = || match kind {
            "Expr" => Sites::call(words),
            "Assign" => Sites::call(&words[equals[equals.len() - 1] + 1..]),
            "AnnAssign" => equals.first().and_then(|&equals| Sites::call(&words[equals + 1..])),
            _ => None,
        }.map(|name| format!("{}()", name.as_deref().unwrap_or("call")));
        const WORDS: [&str; 18] = ["return", "raise", "assert", "import", "from", "pass", "break", "continue", "del", "global", "nonlocal", "yield",
                                   "while", "with", "match", "case", "async", "await"];
        // What the tree holds of it: the value a name is assigned, or the call it is.
        let value = match kind {
            _ if self.tree.is_none() => None,
            "Assign" | "AnnAssign" if name.is_some() => Some(&words[equals[0] + 1..]),
            "Expr" if Sites::call(words).is_some() => Some(words),
            _ => None,
        };
        let key = name.or_else(call).unwrap_or_else(|| {
            let starts = matches!(first.3, scan::NAME | scan::KEYWORD) && WORDS.contains(&first.4);
            format!("##{}", if starts { first.4 } else { "statement" })
        });
        let around = self.open.last_mut().unwrap();
        let mut path = around.path.clone();
        path.push(numbered(&mut around.seen, key, "#"));
        if let (Some(tree), Some(value)) = (self.tree.as_mut(), value) { tree.simple(&path, value); }
        self.found.push((first.0, first.1, last.0, last.2, kind, path));
    }
}

/// An entry of the symbol table (`Facts::symbols`); `kind` 0 class, 1 def, 2 var.
struct Entry {
    dotted: String,
    name: String,
    kind: u8,
    line: u32,
    end: u32,
    indent: u32,
    column: u32,
    parent: i32,
    shown: String,
    tint: Vec<f64>,
}

/// The symbol table: its entries, the entry of each definition (-1: none),
/// and the first entry of each name in each entry (-1: the module).
struct Symbols {
    entries: Vec<Entry>,
    of: Vec<i32>,
    index: HashMap<(i32, String), usize>,
    /// The lines that mention a tint: nothing else is looked at for one.
    hints: Vec<u32>,
}

impl Symbols {
    fn hinted(&self, first: u32, last: u32) -> bool {
        self.hints.get(self.hints.partition_point(|&line| line < first)).is_some_and(|&line| line <= last)
    }
}

/// The colour a `tint=(r, g, b)` or `tint=(r, g, b, a)` in `text` states: its red, green and blue.
fn tint_in(text: &str) -> Option<Vec<f64>> {
    let mut from = 0;
    while let Some(at) = text[from..].find("tint").map(|at| from + at) {
        from = at + 4;
        if text[..at].chars().next_back().is_some_and(|c| c.is_alphanumeric() || c == '_') { continue; }
        let Some(rest) = text[from..].trim_start().strip_prefix('=').and_then(|rest| rest.trim_start().strip_prefix('(')) else { continue };
        let mut parts: Vec<&str> = rest[..rest.find(')')?].split(',').map(str::trim).collect();
        if parts.last() == Some(&"") { parts.pop(); }
        let numbers: Option<Vec<f64>> = parts.iter().map(|part| part.replace('_', "").parse().ok()).collect();
        return numbers.filter(|numbers| (3..=4).contains(&numbers.len())).map(|numbers| numbers[..3].to_vec());
    }
    None
}

/// The scopes the line last looked at sees, innermost first.
struct Sight {
    line: u32,
    scopes: Vec<i32>,
}

impl Sight {
    fn at(&mut self, facts: &Facts, row: u32) -> &[i32] {
        if row != self.line {
            self.line = row;
            // A definition written on one line: its body is on the header's line.
            let opened = facts.defs.partition_point(|def| def.line < row);
            let inner = if facts.defs.get(opened).is_some_and(|def| def.line == row) { opened as i32 } else { facts.within(row) };
            self.scopes = facts.chain(inner);
        }
        &self.scopes
    }
}

/// A `*` argument being read: whether it is a tuple or list written out, and of which elements.
#[derive(Default)]
struct Splat {
    seen: u32,
    /// The literal type of each element read, and of the one being read (`tail` tokens of it so far).
    kinds: Vec<u8>,
    state: u8,
    tail: usize,
    round: bool,
    done: bool,
    odd: bool,
}

impl Splat {
    /// The next token after the star, `depth` brackets inside the call.
    fn read(&mut self, depth: i32, kind: u8, op: u8, word: &str) {
        let ends = depth == 1 && matches!(op, b')' | b']' | b',');
        if self.seen == 0 {
            (self.round, self.odd) = (op == b'(', !matches!(op, b'(' | b'['));
        } else if self.done {
            self.odd = true;
        } else if ends {
            // (`(x)` is no tuple.)
            self.odd |= self.round && op == b')' && self.kinds.is_empty() && self.tail > 0;
            if self.tail > 0 { self.kinds.push(call::settled(self.state)); }
            (self.state, self.tail, self.done) = (0, 0, op != b',');
        } else {
            self.odd |= (self.tail == 0 && (op == b'*' || word == "**")) || (kind == scan::KEYWORD && matches!(word, "for" | "lambda"));
            self.state = call::literal(self.state, kind, word);
            self.tail += 1;
        }
        self.seen += 1;
    }
}

fn word<'a>(text: &'a str, tok: &Tok) -> &'a str {
    &text[tok.start as usize..tok.start as usize + tok.len as usize]
}

/// Whether `name` holds `needle` (lower case), case blind.
fn holds(name: &str, needle: &str) -> bool {
    if name.is_ascii() {
        needle.is_empty() || name.as_bytes().windows(needle.len()).any(|part| part.eq_ignore_ascii_case(needle.as_bytes()))
    } else {
        name.to_lowercase().contains(needle)
    }
}

struct Open {
    indent: u32,
    head: u32,
    spaces: u32,
    /// The names of the definitions around, hashed onto "scope" and onto "block".
    scope: u64,
    block: u64,
    /// A block's own key.
    key: u64,
    /// Index of this definition in `defs`, or -1 for a block.
    def: i32,
    /// The innermost definition: this one, or the one a block sits in (-1: none).
    owner: i32,
}

struct Row {
    head: u32,
    last: u32,
    key: u64,
    twin: u32,
    owner: i32,
    indent: u32,
    kind: u8,
    default: bool,
}

#[derive(Default)]
struct Reader {
    rows: Vec<Row>,
    count: Keyed<u32>,
}

impl Reader {
    fn emit(&mut self, head: u32, last: u32, kind: u8, key: u64, owner: i32, indent: u32) {
        let seen = self.count.entry(key).or_insert(0);
        let twin = *seen;
        *seen += 1;
        let default = matches!(kind, STR | COMMENT | IMPORTS);
        self.rows.push(Row { head, last, key: twinned(key, twin), twin, owner, indent, kind, default });
    }
}

/// The index of the range around each of `ranges` (-1: none), which are
/// `(head, last)` sorted outermost first.
pub fn nest(ranges: impl Iterator<Item = (u32, u32)>) -> Vec<i32> {
    let (mut parents, mut open): (Vec<i32>, Vec<(i32, u32)>) = (vec![], vec![]);
    for (index, (head, last)) in ranges.enumerate() {
        while open.last().is_some_and(|&(_, end)| end < head) { open.pop(); }
        parents.push(open.last().map_or(-1, |&(outer, _)| outer));
        open.push((index as i32, last));
    }
    parents
}

impl Facts {
    /// Read the facts of `chunks`. `blocks`: compound statements fold too.
    pub fn read(chunks: Vec<Arc<Chunk>>, version: u64, blocks: bool) -> Facts {
        let mut facts = Facts { version, line0: Vec::with_capacity(chunks.len() + 1), ..Facts::default() };
        let mut reader = Reader::default();
        let mut defs: Vec<Def> = vec![];
        let mut stack: Vec<Open> = vec![];
        let kind = |kind: u8| mix(SEED, hash(KINDS[kind as usize]));
        let mut last_code: i64 = -1;
        let mut run: Option<(u32, bool)> = None;       // (first line, it is metadata) of the comment run
        let mut run_indent = 0;
        let mut string_head = 0;                       // where the open string started
        let (mut import_first, mut import_last): (Option<u32>, u32) = (None, 0);
        let (mut import_done, mut import_more, mut import_depth) = (false, false, 0i32);
        let close = |open: Open, last_code: i64, reader: &mut Reader, defs: &mut Vec<Def>| {
            let last = last_code.max(open.head as i64) as u32;
            if open.def >= 0 { defs[open.def as usize].last = last; }
            if last > open.head {
                let (kind, key) = if open.def >= 0 { (SCOPE, open.scope) } else { (BLOCK, open.key) };
                reader.emit(open.head, last, kind, key, open.owner, open.spaces);
            }
        };
        // A string or comment run `start ..= end` ended; they are named by their first line.
        let titled = |start: u32, end: i64, kind_: u8, facts: &Facts, chunks: &[Arc<Chunk>], reader: &mut Reader| {
            let key = mix(kind(kind_), hash(strip(text(chunks, &facts.line0, start))));
            reader.emit(start, end as u32, kind_, key, -1, 0);
        };
        // Two comment lines or more fold, and so does a lone metadata comment.
        let comments = |run: Option<(u32, bool)>, end: i64, facts: &Facts, chunks: &[Arc<Chunk>], reader: &mut Reader| {
            if let Some((start, meta)) = run {
                if end > start as i64 || (end == start as i64 && meta) { titled(start, end, COMMENT, facts, chunks, reader); }
            }
        };
        // The binder of a statement left open by the chunk above, and the function whose header was read last.
        let mut open: Option<Binder> = None;
        let mut header = -1;
        let mut binds: Vec<Binding> = vec![];
        let mut entry = 0u8;
        let mut at = 0u32;
        let mut heads = 0;
        for (index, chunk) in chunks.iter().enumerate() {
            let scan = chunk.scan();
            let strings = chunk.strings(entry);
            facts.entries.push(entry);
            // The definitions open here (their indexes and indents), and the index the next one gets.
            facts.opens.push(stack.iter().filter(|open| open.def >= 0).fold(defs.len() as u64, |key, open| mix(mix(key, open.def as u64), open.indent as u64)));
            let read = open.take().map(|binder| Bound::read(&chunk.text, scan, strings, binder));
            let bound = read.as_ref().unwrap_or_else(|| chunk.bound(entry));
            let mut next = 0;
            let mut names = scan.names.iter().peekable();
            facts.line0.push(at);
            for (local, line) in scan.lines.iter().enumerate() {
                // The line's bindings, once its scope is known (when the line is left).
                let from = next;
                while next < bound.binds.len() && bound.binds[next].line as usize == local { next += 1; }
                let place = |owner: i32, inner: i32, header: i32, at: u32, binds: &mut Vec<Binding>| {
                    binds.extend(bound.binds[from..next].iter().map(|found| Binding {
                        scope: match found.scope {
                            bind::OPENED if inner >= 0 => inner,
                            bind::HEADER if header >= 0 => header,
                            _ => owner,
                        },
                        line: at, chunk: index as u32, ..found.clone()
                    }));
                };
                let state = strings.lines[local];
                let blank = line.flags & scan::BLANK != 0;
                if state & 1 != 0 {
                    // The line began inside a string: opaque to everything else.
                    if state & 2 != 0 && at > string_head + 1 {
                        titled(string_head, at as i64 - 1, STR, &facts, &chunks[..=index], &mut reader);
                    }
                    if state >> 2 != 0 && state & 2 != 0 { string_head = at; }
                    comments(run.take(), at as i64 - 1, &facts, &chunks[..=index], &mut reader);
                    if !blank { last_code = at as i64; }
                    place(stack.last().map_or(-1, |open| open.owner), -1, header, at, &mut binds);
                    at += 1;
                    continue;
                }
                if state >> 2 != 0 { string_head = at; }
                let comment = line.flags & scan::COMMENT != 0;
                if comment {
                    if run.is_some() && line.indent != run_indent {
                        comments(run.take(), at as i64 - 1, &facts, &chunks[..=index], &mut reader);
                    }
                    if run.is_none() {
                        run = Some((at, line.flags & scan::META != 0));
                        run_indent = line.indent;
                    }
                } else {
                    comments(run.take(), at as i64 - 1, &facts, &chunks[..=index], &mut reader);
                }
                if blank {
                    at += 1;
                    continue;
                }
                // A line inside open brackets continues a statement: it ends no scope and opens none.
                let continued = bound.continued[local];
                let opens = line.flags & (scan::DEF | scan::CLASS) != 0 || (blocks && line.flags & scan::BLOCK != 0);
                if continued {
                    facts.bent += (opens || stack.last().is_some_and(|open| line.indent <= open.indent)) as u32;
                } else {
                    while stack.last().is_some_and(|open| line.indent <= open.indent) {
                        close(stack.pop().unwrap(), last_code, &mut reader, &mut defs);
                    }
                }
                if !import_done && !comment {
                    let slash = line.flags & scan::SLASH != 0;
                    if import_more {
                        import_last = at;
                        import_depth += line.parens;
                        import_more = import_depth > 0 || slash;
                    } else if line.indent == 0 && line.flags & scan::IMPORT != 0 {
                        import_first.get_or_insert(at);
                        import_last = at;
                        import_depth = line.parens;
                        import_more = import_depth > 0 || slash;
                    } else if import_first.is_some() && line.indent == 0 {
                        import_done = true;
                    }
                }
                let (scope, block, owner) = stack.last()
                    .map_or((kind(SCOPE), kind(BLOCK), -1), |open| (open.scope, open.block, open.owner));
                let open = Open { indent: line.indent, head: at, spaces: line.spaces, scope, block, key: 0, def: -1, owner };
                let mut inner = -1;
                if continued {
                } else if line.flags & (scan::DEF | scan::CLASS) != 0 {
                    while names.peek().is_some_and(|(owner, _)| (*owner as usize) < local) { names.next(); }
                    let name = hash(names.next().map_or("?", |(_, name)| name));
                    inner = defs.len() as i32;
                    if line.flags & scan::DEF != 0 { header = inner; }
                    defs.push(Def { class: line.flags & scan::CLASS != 0, line: at, last: at, indent: line.indent, parent: owner });
                    stack.push(Open { scope: mix(scope, name), block: mix(block, name), def: inner, owner: inner, ..open });
                } else if opens {
                    let key = mix(mix(block, hash(KEYWORDS[line.kw as usize - 1])), hash(strip(chunk.line(local))));
                    stack.push(Open { key, ..open });
                }
                place(owner, inner, header, at, &mut binds);
                last_code = at as i64;
                at += 1;
            }
            entry = strings.exit;
            facts.star |= bound.star;
            heads = mix(heads, bound.heads);
            for flaw in &bound.flaws {
                let row = |line: i32| (at as i64 - scan.lines.len() as i64 + line as i64).max(0) as u32;
                facts.flaws.push((row(flaw.line), match flaw.what {
                    bind::NEVER => format!("'{}' was never closed", flaw.bracket as char),
                    bind::UNMATCHED => format!("unmatched '{}'", flaw.bracket as char),
                    _ => format!("closing parenthesis '{}' does not match opening parenthesis '{}'{}", flaw.bracket as char, flaw.other as char,
                                 if flaw.from == flaw.line { String::new() } else { format!(" on line {}", row(flaw.from) + 1) }),
                }));
            }
            open = bound.open.as_ref().map(|binder| binder.resume(scan.lines.len()));
            facts.live.extend(read.map(|read| (index as u32, read)));
        }
        facts.line0.push(at);
        // Where the text ends: a bracket or a triple-quoted string still open.
        if let Some(&(bracket, line)) = open.as_ref().and_then(|binder| binder.stack.last()) {
            facts.flaws.push(((at as i64 + line as i64).max(0) as u32, format!("'{}' was never closed", bracket as char)));
        }
        if entry != 0 {
            let kind = if (entry - 1) / 2 == 2 { "f-string" } else { "string" };
            facts.flaws.push((string_head, format!("unterminated triple-quoted {kind} literal (detected at line {})", at.max(1))));
        }
        facts.flaws.sort();
        comments(run.take(), at as i64 - 1, &facts, &chunks, &mut reader);
        for open in stack.drain(..) {
            close(open, last_code, &mut reader, &mut defs);
        }
        if let Some(first) = import_first {
            if import_last > first { reader.emit(first, import_last, IMPORTS, kind(IMPORTS), -1, 0); }
        }
        // Outermost first. Ranges on the same lines keep the order they were found in.
        let mut order: Vec<(u64, u32)> = reader.rows.iter().enumerate()
            .map(|(index, row)| ((row.head as u64) << 32 | (u32::MAX - row.last) as u64, index as u32)).collect();
        order.sort_unstable();
        for (_, index) in order {
            let row = &reader.rows[index as usize];
            if row.last == row.head {
                facts.metas.push((row.head, row.twin));
                continue;
            }
            if row.default { facts.defaults.push(facts.heads.len() as u32); }
            facts.heads.push(row.head);
            facts.spans.push(row.last - row.head);
            facts.keys.push(row.key);
            facts.kinds.push(row.kind);
            facts.indents.push(row.indent);
            facts.twins.push(row.twin);
            facts.owners.push(row.owner);
        }
        facts.parents = nest(facts.heads.iter().zip(&facts.spans).map(|(&head, &span)| (head, head + span)));
        // Each scope's names, once each, in the order they are bound.
        let mut seen = Keyed::<()>::default();
        let mut unique = vec![];
        facts.first = vec![0; defs.len() + 2];
        let mut print = 0u64;
        for (index, found) in binds.iter().enumerate() {
            print = print.wrapping_add(mix(mix(found.hash, found.scope as u64), found.kind as u64));
            // (`global` binds in the module too.)
            for scope in [Some(found.scope), (found.kind == bind::GLOBAL).then_some(-1)].into_iter().flatten() {
                let import = |at: u32| at == BROUGHT || binds.get(at as usize).is_some_and(|other| other.kind == bind::IMPORT);
                facts.bound.entry(mix(found.hash, scope as u64))
                    .and_modify(|only| *only = if found.kind == bind::IMPORT && import(*only) { BROUGHT } else { MANY })
                    .or_insert(index as u32);
            }
            if found.kind < bind::GLOBAL && seen.insert(mix(found.hash, found.scope as u64), ()).is_none() {
                unique.push(index as u32);
                facts.first[(found.scope + 2) as usize] += 1;
            }
        }
        for scope in 1..facts.first.len() { facts.first[scope] += facts.first[scope - 1]; }
        let mut next = facts.first.clone();
        facts.names = vec![0; unique.len()];
        for index in unique {
            let slot = &mut next[(binds[index as usize].scope + 1) as usize];
            facts.names[*slot as usize] = index;
            *slot += 1;
        }
        facts.print = mix(mix(print, defs.len() as u64), heads);
        facts.binds = binds;
        facts.defs = defs;
        facts.chunks = chunks;
        facts
    }

    /// What chunk `at` binds, uses and calls.
    fn chunk(&self, at: usize) -> &Bound {
        match self.live.binary_search_by_key(&(at as u32), |(chunk, _)| *chunk) {
            Ok(found) => &self.live[found].1,
            Err(_) => self.chunks[at].bound(self.entries[at]),
        }
    }

    /// The lines `first ..= last` as (chunk index, line in the chunk, line).
    fn rows(&self, first: u32, last: u32) -> impl Iterator<Item = (usize, usize, u32)> + '_ {
        let lines = *self.line0.last().unwrap_or(&0);
        (first..last.saturating_add(1).min(lines)).map(|line| {
            let at = self.line0[..self.chunks.len()].partition_point(|&start| start <= line) - 1;
            (at, (line - self.line0[at]) as usize, line)
        })
    }

    /// The column (in characters) of byte `start` of chunk `at`, which is on its line `local`.
    fn chars(&self, at: usize, local: usize, start: u32) -> u32 {
        let chunk = &self.chunks[at];
        chunk.text[chunk.scan().lines[local].start as usize..start as usize].chars().count() as u32
    }

    /// The names used on lines `first ..= last`: (line, column, dotted chain, the statement binds it).
    pub fn references(&self, first: u32, last: u32) -> Vec<(u32, u32, &str, bool)> {
        let mut found = vec![];
        for (at, local, line) in self.rows(first, last) {
            let (bound, text) = (self.chunk(at), &self.chunks[at].text);
            let from = bound.refs.partition_point(|used| (used.line as usize) < local);
            found.extend(bound.refs[from..].iter().take_while(|used| used.line as usize == local).map(|used| {
                (line, self.chars(at, local, used.start), &text[used.start as usize..used.start as usize + used.len as usize], used.store)
            }));
        }
        found
    }

    /// The calls of dotted chains that start on lines `first ..= last`:
    /// (line, column, chain, positional arguments or -1 when the call does
    /// not close within `REACH` lines, keyword names, a `*` argument, a `**` argument).
    pub fn calls(&self, first: u32, last: u32) -> Vec<(u32, u32, &str, i32, Vec<&str>, bool, bool)> {
        let mut found = vec![];
        for (at, local, line) in self.rows(first, last) {
            let (bound, text) = (self.chunk(at), &self.chunks[at].text);
            let from = bound.calls.partition_point(|call| (call.line as usize) < local);
            for call in bound.calls[from..].iter().take_while(|call| call.line as usize == local) {
                let chain = &text[call.start as usize..call.start as usize + call.len as usize];
                let (positional, keys, stars) = self.arguments(line, call.paren).map_or((-1, vec![], 0), |args| {
                    ((args.plain.len() - args.splat) as i32, args.keys.iter().map(|key| key.0).collect(), args.stars)
                });
                found.push((line, self.chars(at, local, call.start), chain, positional, keys, stars & 1 != 0, stars & 2 != 0));
            }
        }
        found
    }

    /// The lint of the whole text: the names used that nothing binds as
    /// (line, column, name, it is in `live`, it stands in an annotation) and
    /// the calls whose arguments do not fit what they call as (line, column,
    /// message), up to `MOST` each; the chains called that `specs` does not
    /// have yet; and the chunks that were read for it.
    ///
    /// A chunk's findings are kept on the chunk and taken again while what
    /// they depend on is the same: the chunk and the string state it is
    /// entered in, the definitions open where it starts, every binding of
    /// the text and every header (`print`), and the signatures told
    /// (`serial`); and the chunks around it when a statement is open where
    /// it starts or a call of it closes further down (up to `AROUND`). So a
    /// reading after an edit that binds nothing new reads the edited chunk
    /// only; one that does reads them all. The `try` guards are applied
    /// here, to what is found.
    ///
    /// A name is bound when its own scope or a function around it binds it
    /// anywhere (a class only for its own body), or `known` has it
    /// (builtins). Errs on silence: no name is reported when a `from x
    /// import *` is about, or in the body of a `try` whose `except` catches
    /// NameError. (An annotation is not evaluated: a name missing there is
    /// no error, but an import would still bring it.) For calls see `misfits`.
    pub fn lint(&self, known: &HashSet<String>, live: &HashSet<String>, specs: &Specs, serial: u64) -> Linted {
        let (mut undefined, mut miscalls, mut wanted, mut fresh) = (vec![], vec![], vec![], 0);
        let heads: Vec<OnceCell<Head>> = self.defs.iter().map(|_| OnceCell::new()).collect();
        let head = |at: usize| heads[at].get_or_init(|| self.head(at));
        let told = OnceCell::new();
        let told = || told.get_or_init(|| specs.iter().map(|(chain, rows)| (chain.as_str(), rows.as_ref().map(|rows| {
            rows.iter().map(|(name, sort, needed, kind)| Param {
                name, line: 0, column: 0, default: (!needed).then_some((0, 0, 0, 0)), star: sort.saturating_sub(2),
                only: *sort == 2, positional: *sort == 0, kind: call::kind(kind),
            }).collect()
        }))).collect::<HashMap<&str, Option<Vec<Param>>>>());
        let (names, types) = (OnceCell::new(), OnceCell::new());
        let free = |guard: &OnceCell<Vec<(u32, u32)>>, errors: &[&str], row: u32| {
            !guard.get_or_init(|| self.guarded(errors)).iter().any(|&(first, last)| first < row && row < last)
        };
        let all = mix(self.print, serial);
        // The chunks before this one that a statement open at its start began in.
        let (mut back, mut open) = (0, self.live.iter().map(|(chunk, _)| *chunk as usize).peekable());
        for (at, chunk) in self.chunks.iter().enumerate() {
            back = if open.next_if_eq(&at).is_some() { back + 1 } else { 0 };
            let key = mix(mix(all, self.opens[at]), self.entries[at] as u64);
            let around = |ahead: u32| {
                let last = (at + ahead as usize).min(self.chunks.len() - 1);
                (at - back as usize..=last).filter(|&other| other != at).fold(key, |key, other| mix(mix(key, self.chunks[other].id), self.entries[other] as u64))
            };
            let kept = chunk.lint.lock().unwrap().clone().filter(|kept| kept.back == back && kept.key == around(kept.ahead));
            let found = kept.unwrap_or_else(|| {
                fresh += 1;
                let (mut found, far) = self.look(at, known, told(), &head);
                let ahead = (self.line0[..self.chunks.len()].partition_point(|&start| start <= far).max(at + 1) - 1 - at) as u32;
                (found.key, found.back, found.ahead) = (around(ahead), back, ahead);
                let found = Arc::new(found);
                if back.max(ahead) <= AROUND { *chunk.lint.lock().unwrap() = Some(found.clone()); }
                found
            });
            for &(local, start, len, note) in found.names.iter().filter(|_| !self.star) {
                let row = self.line0[at] + local;
                if undefined.len() < MOST && free(&names, &["NameError"], row) {
                    let name = &chunk.text[start as usize..start as usize + len as usize];
                    undefined.push((row, self.chars(at, local as usize, start), name.to_string(), live.contains(name), note));
                }
            }
            for (local, start, message, told) in &found.calls {
                let row = self.line0[at] + local;
                if miscalls.len() < MOST && (!told || free(&types, &["TypeError", "AttributeError"], row)) {
                    miscalls.push((row, self.chars(at, *local as usize, *start), message.clone()));
                }
            }
            for chain in &found.wanted {
                if !wanted.contains(chain) { wanted.push(chain.clone()); }
            }
        }
        wanted.sort_unstable();
        (undefined, miscalls, wanted, fresh)
    }

    /// The lint findings of chunk `at` (see `lint`), and the last line a call in it was read to.
    fn look<'a>(&'a self, at: usize, known: &HashSet<String>, told: &HashMap<&str, Option<Vec<Param>>>,
                head: &impl Fn(usize) -> &'a Head<'a>) -> (Lint, u32) {
        let mut found = Lint { key: 0, back: 0, ahead: 0, names: vec![], calls: vec![], wanted: vec![] };
        let (bound, text) = (self.chunk(at), &self.chunks[at].text);
        let mut sight = Sight { line: u32::MAX, scopes: vec![] };
        for used in bound.refs.iter().filter(|used| !used.store) {
            let chain = &text[used.start as usize..used.start as usize + used.len as usize];
            let name = chain.split(|c: char| c == '.' || c.is_whitespace()).next().unwrap_or(chain);
            if known.contains(name) { continue; }
            let key = hash(name);
            if !sight.at(self, self.line0[at] + used.line).iter().any(|&scope| self.bound.contains_key(&mix(key, scope as u64))) {
                found.names.push((used.line, used.start, name.len() as u16, used.note));
            }
        }
        let far = self.misfits(at, &mut found, &mut sight, told, head);
        (found, far)
    }

    /// The innermost definition whose body holds `line`, or -1.
    fn within(&self, line: u32) -> i32 {
        let mut at = self.defs.partition_point(|def| def.line < line) as i32 - 1;
        while at >= 0 && self.defs[at as usize].last < line { at = self.defs[at as usize].parent; }
        at
    }

    /// The bodies of `try` statements whose handlers catch one of `errors`
    /// (or everything): (the `try` line, its first handler's line).
    fn guarded(&self, errors: &[&str]) -> Vec<(u32, u32)> {
        const TRY: u8 = 6;
        let (mut found, mut open): (Vec<(u32, u32)>, Vec<(u32, u32, u32, bool)>) = (vec![], vec![]);
        for (at, chunk) in self.chunks.iter().enumerate() {
            let (scan, strings, bound) = (chunk.scan(), chunk.strings(self.entries[at]), self.chunk(at));
            for (local, line) in scan.lines.iter().enumerate() {
                if line.flags & (scan::BLANK | scan::COMMENT) != 0 || strings.lines[local] & 1 != 0 || bound.continued[local] { continue; }
                let row = self.line0[at] + local as u32;
                let toks = strings.on(local);
                let head = toks.first().map_or("", |tok| &chunk.text[tok.start as usize..tok.start as usize + tok.len as usize]);
                // (indent, the `try` line, its first handler's line, a handler catches one of them)
                while open.last().is_some_and(|top| top.0 > line.indent || (top.0 == line.indent && !matches!(head, "except" | "else" | "finally"))) {
                    let (_, first, last, guard) = open.pop().unwrap();
                    if guard { found.push((first, last)); }
                }
                if line.kw == TRY && line.flags & scan::BLOCK != 0 {
                    open.push((line.indent, row, u32::MAX, false));
                } else if head == "except" {
                    if let Some(top) = open.last_mut().filter(|top| top.0 == line.indent) {
                        top.2 = top.2.min(row);
                        // A bare `except:` catches it, and so does one that names it before any `as`.
                        let word = |tok: &scan::Tok| &chunk.text[tok.start as usize..tok.start as usize + tok.len as usize];
                        let caught = toks.iter().take_while(|tok| !(tok.kind == scan::KEYWORD && word(tok) == "as"))
                            .any(|tok| tok.kind == scan::NAME && (errors.contains(&word(tok)) || matches!(word(tok), "Exception" | "BaseException")));
                        top.3 |= caught || toks.iter().filter(|tok| tok.kind != scan::NOTE).count() <= 2;
                    }
                }
            }
        }
        found.extend(open.into_iter().filter(|top| top.3).map(|top| (top.1, top.2)));
        found
    }

    /// The statements that start on lines `first ..= last` as (first line,
    /// last line): a statement goes on over the lines its brackets, strings
    /// and backslashes continue it on. A compound statement is its header.
    pub fn statements(&self, first: u32, last: u32) -> Vec<(u32, u32)> {
        // Whether a line belongs to the statement above it.
        let goes_on = |at: usize, local: usize| self.chunk(at).continued[local] || self.chunks[at].strings(self.entries[at]).lines[local] & 1 != 0;
        let mut found = vec![];
        for (at, local, line) in self.rows(first, last) {
            if self.chunks[at].scan().lines[local].flags & (scan::BLANK | scan::COMMENT) != 0 || goes_on(at, local) { continue; }
            let end = self.rows(line + 1, line + LONG).take_while(|&(at, local, _)| goes_on(at, local)).last().map_or(line, |row| row.2);
            found.push((line, end));
        }
        found
    }

    /// The parameters of the function whose `def` is on `line`: (name,
    /// line, column, where its default value is as (line, column, last line,
    /// end column) or None).
    #[allow(clippy::type_complexity)]
    pub fn parameters(&self, line: u32) -> Vec<(&str, u32, u32, Option<(u32, u32, u32, u32)>)> {
        self.header(line).0.into_iter().map(|param| (param.name, param.line, param.column, param.default)).collect()
    }

    /// The parameters of the function whose `def` is on `line`, and whether the bracket around them closes.
    fn header(&self, line: u32) -> (Vec<Param>, bool) {
        let mut found: Vec<Param> = vec![];
        if !self.rows(line, line).any(|(at, local, _)| self.chunks[at].scan().lines[local].flags & scan::DEF != 0) { return (found, false); }
        // Brackets open (0: not in the header's yet); a parameter starts at the next token;
        // a default value is being read; a lambda's parameters are being read.
        let (mut depth, mut fresh, mut value, mut lambda) = (0, false, false, false);
        // The parameters from here on are keyword only; the stars before the name; the tokens of its annotation.
        let (mut only, mut star, mut noted) = (false, 0u8, 0);
        for (at, local, row) in self.rows(line, line + REACH) {
            // (The header is one statement, as the binder reads statements: `Bound::heads` hashes its lines.)
            if row > line && !self.chunk(at).continued[local] { break; }
            let text = &self.chunks[at].text;
            let toks = self.chunks[at].strings(self.entries[at]).on(local);
            for tok in toks.iter().filter(|tok| tok.kind != scan::NOTE) {
                let word = word(text, tok);
                let op = if tok.kind == scan::OP { word.as_bytes()[0] } else { 0 };
                let (column, end) = (self.chars(at, local, tok.start), self.chars(at, local, tok.start + tok.len as u32));
                if depth == 1 && matches!(op, b')' | b',') && !(lambda && op == b',') {
                    only |= star == 1;
                    (fresh, value, lambda, star, noted) = (true, false, false, 0, 0);
                    if op == b')' { return (found, true); }
                    continue;
                }
                if depth == 1 && fresh {
                    match (tok.kind, op, word) {
                        (scan::NAME, ..) => {
                            found.push(Param { name: word, line: row, column, default: None, star, only: only && star == 0, positional: false, kind: 0 });
                            fresh = false;
                        }
                        (_, b'*', _) => star = 1,
                        (_, _, "**") => star = 2,
                        (_, b'/', _) => found.iter_mut().for_each(|param| param.positional = true),
                        _ => {}
                    }
                } else if depth == 1 && op == b'=' && !value {
                    value = true;
                } else if value {
                    if let Some(last) = found.last_mut() {
                        let start = last.default.map_or((row, column), |span| (span.0, span.1));
                        last.default = Some((start.0, start.1, row, end));
                    }
                    lambda |= depth == 1 && tok.kind == scan::KEYWORD && word == "lambda";
                    lambda &= !(depth == 1 && op == b':');
                } else if depth >= 1 {
                    // Its annotation: `: name` and nothing more is a type.
                    noted += 1;
                    if let Some(last) = found.last_mut() { last.kind = if noted == 2 && tok.kind == scan::NAME { call::kind(word) } else { 0 }; }
                }
                match op {
                    b'(' | b'[' | b'{' => {
                        depth += 1;
                        fresh |= depth == 1;
                    }
                    b')' | b']' | b'}' => depth -= 1,
                    _ => {}
                }
            }
        }
        (found, false)
    }

    /// What the decorators above the definition on `line` leave of its
    /// signature (`call::BARE` ..): `@staticmethod` or `@classmethod` alone,
    /// or only decorators of `call::TRANSPARENT`, plain or called.
    fn flavor(&self, line: u32) -> u8 {
        let (mut count, mut plain, mut sole) = (0, true, call::OPAQUE);
        // The statement above is read from its first line: `below` is where the one read last starts.
        let (mut row, mut below) = (line, line);
        while row > 0 {
            row -= 1;
            let (chunk, local) = place(&self.chunks, &self.line0, row);
            let at = self.line0[..self.chunks.len()].partition_point(|&start| start <= row) - 1;
            if chunk.strings(self.entries[at]).lines[local] & 1 != 0 { break; }
            if chunk.scan().lines[local].flags & (scan::BLANK | scan::COMMENT) != 0 || self.chunk(at).continued[local] { continue; }
            let mut toks = self.rows(row, below - 1).flat_map(|(at, local, _)| {
                let chunk = &self.chunks[at];
                chunk.strings(self.entries[at]).on(local).iter().filter(|tok| tok.kind != scan::NOTE).map(move |tok| (tok.kind, word(&chunk.text, tok)))
            });
            if toks.next() != Some((scan::OP, "@")) { break; }
            let name = toks.next().filter(|tok| tok.0 == scan::NAME).map_or("", |tok| tok.1);
            // Anything after the name but one call of it: unknown.
            let (mut depth, mut called, mut odd) = (0, false, name.is_empty());
            for (kind, word) in toks {
                odd |= depth == 0 && (called || word != "(");
                if kind == scan::OP && matches!(word, "(" | "[" | "{") {
                    depth += 1;
                } else if kind == scan::OP && matches!(word, ")" | "]" | "}") {
                    depth -= 1;
                    called |= depth == 0;
                }
            }
            count += 1;
            plain &= !odd && call::TRANSPARENT.contains(&name);
            sole = match name {
                "staticmethod" if !odd && !called => call::STATIC,
                "classmethod" if !odd && !called => call::CLASS,
                _ => call::OPAQUE,
            };
            below = row;
        }
        match count {
            0 => call::BARE,
            1 if sole != call::OPAQUE => sole,
            _ if plain => call::PLAIN,
            _ => call::OPAQUE,
        }
    }

    /// The header of definition `at`.
    fn head(&self, at: usize) -> Head {
        let (params, closed) = self.header(self.defs[at].line);
        Head { params, closed, flavor: self.flavor(self.defs[at].line) }
    }

    /// The arguments after the opening bracket at byte `paren` of `line`'s
    /// chunk, or None when the call does not close within `REACH` lines. A
    /// generator expression is one argument.
    fn arguments(&self, line: u32, paren: u32) -> Option<Args> {
        let mut args = Args::default();
        // Brackets open inside the call; an argument starts at the next token; the
        // arguments are one generator expression; a lambda's parameters are being read.
        let (mut depth, mut fresh, mut whole, mut lambda) = (0, true, false, false);
        // The argument being read: 0 positional, 1 keyword, 2 `*`, 3 `**`; the literal it
        // spells so far; tokens before its value; what a `*` is given.
        let (mut sort, mut state, mut skip, mut splat) = (0, 0, 0, Splat::default());
        for (at, local, row) in self.rows(line, line + REACH) {
            let text = &self.chunks[at].text;
            let toks = self.chunks[at].strings(self.entries[at]).on(local);
            for (index, tok) in toks.iter().enumerate() {
                if (row == line && tok.start <= paren) || tok.kind == scan::NOTE { continue; }
                let word = word(text, tok);
                let op = if tok.kind == scan::OP { word.as_bytes()[0] } else { 0 };
                if depth == 0 && (op == b')' || (op == b',' && !whole && !lambda)) {
                    (args.last, args.end) = (row, self.chars(at, local, tok.start) + 1);
                    match sort {
                        _ if fresh => {}
                        0 => args.plain.push(call::settled(state)),
                        1 => args.keys.last_mut().unwrap().1 = call::settled(state),
                        2 if splat.done && !splat.odd => {
                            args.splat += splat.kinds.len();
                            args.plain.append(&mut splat.kinds);
                        }
                        2 => args.stars |= 4,
                        _ => {}
                    }
                    fresh = true;
                    if op == b')' { return Some(args); }
                    continue;
                }
                if depth == 0 && fresh {
                    fresh = false;
                    let named = tok.kind == scan::NAME && toks.get(index + 1).is_some_and(|after| after.kind == scan::OP && text.as_bytes()[after.start as usize] == b'=');
                    (sort, skip) = match (op, word) {
                        (b'*', _) => (2, 1),
                        (_, "**") => (3, 1),
                        _ if named => (1, 2),
                        _ => (0, 0),
                    };
                    (state, splat) = (0, Splat::default());
                    args.stars |= [0, 0, 1, 2][sort];
                    if named { args.keys.push((word, 0)); }
                }
                if skip > 0 {
                    skip -= 1;
                } else if sort == 2 {
                    splat.read(depth, tok.kind, op, word);
                } else {
                    state = call::literal(state, tok.kind, word);
                }
                match (op, word) {
                    (b'(' | b'[' | b'{', _) => depth += 1,
                    (b')' | b']' | b'}', _) => depth -= 1,
                    (b':', _) if depth == 0 => lambda = false,
                    (_, "for") if depth == 0 && tok.kind == scan::KEYWORD => whole = true,
                    (_, "lambda") if depth == 0 && tok.kind == scan::KEYWORD => lambda = true,
                    _ => {}
                }
            }
        }
        None
    }

    /// The chunk `line` is in, and the line's tokens.
    fn toks(&self, line: u32) -> (&Chunk, &[Tok]) {
        let at = self.line0[..self.chunks.len()].partition_point(|&start| start <= line) - 1;
        (&self.chunks[at], self.chunks[at].strings(self.entries[at]).on((line - self.line0[at]) as usize))
    }

    /// The definition `binding` (a `def` or `class` line) makes.
    fn made(&self, binding: &Binding) -> Option<usize> {
        let at = self.defs.partition_point(|def| def.line < binding.line);
        (matches!(binding.kind, bind::DEF | bind::CLASS) && self.defs.get(at).is_some_and(|def| def.line == binding.line)).then_some(at)
    }

    /// The one binding of `name` in `scope`: Err when it is bound more than once.
    fn only(&self, name: &str, scope: i32) -> Option<Result<&Binding, ()>> {
        let at = *self.bound.get(&mix(hash(name), scope as u64))?;
        Some(self.binds.get(at as usize).filter(|found| self.word(found) == name).ok_or(()))
    }

    /// What a call of the bare `name` calls, seen from `scopes` (innermost
    /// first): the first scope that binds the name decides. A function when
    /// that is its one binding (not in a class: a method called bare has no
    /// `self`), or a class by its `__init__`.
    fn function<'a>(&'a self, name: &str, scopes: &[i32], head: &impl Fn(usize) -> &'a Head<'a>) -> Option<Callee> {
        let (scope, found) = scopes.iter().find_map(|&scope| self.only(name, scope).map(|found| (scope, found)))?;
        let found = found.ok()?;
        let at = self.made(found)?;
        if found.kind == bind::DEF {
            let class = scope >= 0 && self.defs[scope as usize].class;
            return (!class && head(at).flavor != call::OPAQUE).then(|| Callee::Def(at, head(at).flavor == call::CLASS));
        }
        if head(at).flavor != call::BARE { return None; }
        match self.only("__init__", at as i32) {
            Some(Ok(init)) => {
                let init = self.made(init).filter(|_| init.kind == bind::DEF)?;
                (head(init).flavor <= call::PLAIN).then_some(Callee::Def(init, true))
            }
            Some(Err(())) => None,
            // No `__init__`, no `__new__`, no bases: it takes no arguments.
            None => {
                let (chunk, toks) = self.toks(self.defs[at].line);
                let is = |index: usize, op: &str| toks.get(index).is_some_and(|tok| tok.kind == scan::OP && word(&chunk.text, tok) == op);
                let bare = is(2, ":") || (is(2, "(") && is(3, ")") && is(4, ":"));
                (bare && self.only("__new__", at as i32).is_none()).then_some(Callee::Nothing)
            }
        }
    }

    /// What `base.name(...)` calls when `base` is the `self` of the method
    /// around definition `inner`: a method written in that class itself.
    fn method<'a>(&'a self, base: &str, name: &str, inner: i32, head: &impl Fn(usize) -> &'a Head<'a>) -> Option<Callee> {
        let mut at = inner;
        let class = loop {
            let def = self.defs.get(usize::try_from(at).ok()?)?;
            if !def.class {
                // The first parameter of a method: its `self`. Any other function hides the one around it.
                let own = head(at as usize);
                let method = def.parent >= 0 && self.defs[def.parent as usize].class && own.flavor <= call::PLAIN && own.closed;
                let first = own.params.iter().find(|param| param.star == 0).filter(|_| method)?;
                if first.name == base { break def.parent; }
            }
            at = def.parent;
        };
        let found = self.only(name, class)?.ok()?;
        let at = self.made(found).filter(|_| found.kind == bind::DEF)?;
        (head(at).flavor != call::OPAQUE).then(|| Callee::Def(at, head(at).flavor != call::STATIC))
    }

    /// The calls of chunk `at` whose arguments do not fit what they call,
    /// into `found`; returns the last line one of them was read to. What is
    /// called is, first, written in this text: a bare name calls the
    /// function or class it is bound to once, `self.name` a method of the
    /// class around. Else it is in the running process: a chain from a name
    /// only imports bind, or a builtin; `told` has their signatures, and the
    /// chains it lacks are asked for. Errs on silence (`call::misfit`): a
    /// decorator that may change the signature, a second binding of the
    /// name, a header or call that does not close: nothing is said.
    fn misfits<'a>(&'a self, at: usize, found: &mut Lint, sight: &mut Sight, told: &HashMap<&str, Option<Vec<Param>>>,
                 head: &impl Fn(usize) -> &'a Head<'a>) -> u32 {
        let (bound, text) = (self.chunk(at), &self.chunks[at].text);
        let (mut key, mut far) = (String::new(), 0);
        // The arguments of a call (None: it does not close within `REACH` lines).
        let mut given = |row: u32, paren: u32| {
            let args = self.arguments(row, paren);
            far = far.max(args.as_ref().map_or(row + REACH, |args| args.last));
            args
        };
        for made in &bound.calls {
            let chain = &text[made.start as usize..made.start as usize + made.len as usize];
            let mut parts = chain.split('.').map(str::trim);
            let (base, member, long) = (parts.next().unwrap_or(""), parts.next(), parts.next().is_some());
            let row = self.line0[at] + made.line;
            let scopes = sight.at(self, row);
            let callee = match member {
                _ if long => None,
                None => self.function(base, scopes, head),
                Some(member) if !made.lambda => self.method(base, member, scopes[0], head),
                _ => None,
            };
            let name = chain.rsplit('.').next().unwrap_or(chain).trim();
            let (message, pushed) = if let Some(callee) = callee {
                (match (callee, given(row, made.paren)) {
                    (Callee::Def(def, skip), Some(args)) if head(def).closed => call::misfit(name, &head(def).params, skip, &args),
                    (Callee::Nothing, Some(args)) if !args.plain.is_empty() || !args.keys.is_empty() || args.stars != 0 => {
                        Some(format!("{name}() takes no arguments"))
                    }
                    _ => None,
                }, false)
            } else {
                // In the running process: a chain from a name only imports bind, or a builtin.
                key.clear();
                match scopes.iter().find_map(|&scope| self.bound.get(&mix(hash(base), scope as u64))) {
                    Some(&only) if only == BROUGHT || self.binds.get(only as usize).is_some_and(|found| found.kind == bind::IMPORT) => key.push_str(chain),
                    None if member.is_none() => key.extend(["builtins.", base]),
                    _ => continue,
                }
                if key.contains(char::is_whitespace) { continue; }
                (match told.get(key.as_str()) {
                    Some(Some(params)) => given(row, made.paren).and_then(|args| call::misfit(name, params, false, &args)),
                    Some(None) => None,
                    None => {
                        if !found.wanted.contains(&key) { found.wanted.push(key.clone()); }
                        None
                    }
                }, true)
            };
            // (Once a line, as the oracle reports.)
            if let Some(message) = message.filter(|message| !found.calls.iter().any(|last| last.0 == made.line && last.2 == *message)) {
                found.calls.push((made.line, made.start, message, pushed));
            }
        }
        far
    }

    /// The top-level statements with what belongs to them, as (first line,
    /// last line, key): each compiles on its own (`editor_model.Analysis`).
    /// One starts at a statement in column 0 that is no clause of the one
    /// above (`else`, `except` ..) and does not follow a decorator; the
    /// lines before the first are one too. The key is the same while the
    /// chunks it lies in are: the same text, in the same place in them.
    pub fn tops(&self) -> Vec<(u32, u32, u64)> {
        self.topped(false).into_iter().map(|(first, last, key, _)| (first, last, key)).collect()
    }

    /// `tops`, each with the chunk it starts in (`Chunk::id`); with
    /// `entered` each key also holds the string state its chunks are
    /// entered in, so it is the same while its tokens are: what a tree
    /// part is kept by (`tree.rs`).
    fn topped(&self, entered: bool) -> Vec<(u32, u32, u64, u64)> {
        let (mut starts, mut decorated) = (vec![(0u32, 0usize)], false);
        for (at, chunk) in self.chunks.iter().enumerate() {
            let (scan, strings, bound) = (chunk.scan(), chunk.strings(self.entries[at]), self.chunk(at));
            for (local, line) in scan.lines.iter().enumerate() {
                if line.indent != 0 || line.flags & (scan::BLANK | scan::COMMENT) != 0 || strings.lines[local] & 1 != 0 || bound.continued[local] { continue; }
                let head = strings.on(local).first().map_or("", |tok| word(&chunk.text, tok));
                if !decorated && !matches!(head, "else" | "elif" | "except" | "finally") && self.line0[at] + local as u32 > 0 {
                    starts.push((self.line0[at] + local as u32, at));
                }
                decorated = head == "@";
            }
        }
        let lines = *self.line0.last().unwrap_or(&0);
        (0..starts.len()).map(|index| {
            let ((first, at), next) = (starts[index], starts.get(index + 1).map_or(lines, |next| next.0));
            let last = next.saturating_sub(1).max(first);
            let end = self.line0[..self.chunks.len()].partition_point(|&start| start <= last) - 1;
            let key = (at..=end).fold(mix(first as u64 - self.line0[at] as u64, (last - first) as u64), |key, chunk| {
                let key = mix(key, self.chunks[chunk].id);
                if entered { mix(key, self.entries[chunk] as u64) } else { key }
            });
            (first, last, key, self.chunks[at].id)
        }).collect()
    }

    /// The text of lines `first ..= last`, with their breaks.
    pub fn span(&self, first: u32, last: u32) -> String {
        self.rows(first, last).map(|(at, local, _)| {
            let (chunk, lines) = (&self.chunks[at], &self.chunks[at].scan().lines);
            let end = lines.get(local + 1).map_or(chunk.text.len(), |next| next.start as usize);
            &chunk.text[lines[local].start as usize..end]
        }).collect()
    }

    /// The file's symbol table, as the plain rows a project's roster holds
    /// per file (the port of `symbol_roster.extract_table`): the entries
    /// in line order, the imports as (alias, module as written, name
    /// imported from it or None), and the modules imported whole (`*`).
    ///
    /// An entry is (dotted name from the module down, name, "class" / "def"
    /// / "var", line, last line, leading whitespace of the line, column of
    /// the name, index of the entry it is in or -1, the line's text up to
    /// 160 characters, the tint its source gives it or nothing). See `symbols`.
    #[allow(clippy::type_complexity)]
    pub fn table(&self) -> (Vec<(String, String, &'static str, u32, u32, u32, u32, i32, String, Vec<f64>)>, Vec<(String, String, Option<String>)>, Vec<String>) {
        let entries = self.symbols().entries.into_iter().map(|entry| {
            (entry.dotted, entry.name, ["class", "def", "var"][entry.kind as usize], entry.line, entry.end, entry.indent, entry.column, entry.parent, entry.shown, entry.tint)
        }).collect();
        let (imports, stars) = self.imports();
        (entries, imports, stars)
    }

    /// The entries of the symbol table. Every class and function is one,
    /// at any depth; a "var" is a name assigned first on its line (`name =`,
    /// `name: type =`) in the module or in a class that is in no function,
    /// once per name (as the oracle: unless it is among the 64 entries
    /// before). A definition lasts to the line before the statement that
    /// ends it. The tint is the one the source states (`tint`).
    fn symbols(&self) -> Symbols {
        let lines = *self.line0.last().unwrap_or(&0);
        let row = |line: u32| place(&self.chunks, &self.line0, line);
        let code = |line: u32| {
            let (chunk, local) = row(line);
            chunk.scan().lines[local].flags & (scan::BLANK | scan::COMMENT) == 0
        };
        let blank = |line: u32| {
            let (chunk, local) = row(line);
            chunk.scan().lines[local].flags & scan::BLANK != 0
        };
        let tail = (0..lines).rev().find(|&line| !blank(line)).unwrap_or(0);
        let lead = |line: u32| text(&self.chunks, &self.line0, line).chars().take_while(|c| c.is_whitespace()).count() as u32;
        let shown = |line: u32| text(&self.chunks, &self.line0, line).trim_end().chars().take(160).collect::<String>();
        let column = |line: u32, tok: &Tok| {
            let (chunk, local) = row(line);
            chunk.text[chunk.scan().lines[local].start as usize..tok.start as usize].chars().count() as u32
        };
        let hints: Vec<u32> = (0..lines).filter(|&line| text(&self.chunks, &self.line0, line).contains("tint")).collect();
        // A class in no function: what is assigned in its body is the class's.
        let open: Vec<bool> = (0..self.defs.len()).map(|at| {
            std::iter::successors(Some(at as i32), |&at| (at >= 0).then(|| self.defs[at as usize].parent)).take_while(|&at| at >= 0).all(|at| self.defs[at as usize].class)
        }).collect();
        // Definitions and assignments, in line order: (line, definition or -1, the name's token, scope).
        let mut found: Vec<(u32, i32, Tok, i32)> = vec![];
        for (at, def) in self.defs.iter().enumerate() {
            let (chunk, toks) = self.toks(def.line);
            let name = toks.iter().skip_while(|tok| tok.kind == scan::KEYWORD && word(&chunk.text, tok) != "def" && word(&chunk.text, tok) != "class").nth(1);
            if let Some(name) = name.filter(|tok| tok.kind == scan::NAME) { found.push((def.line, at as i32, *name, def.parent)); }
        }
        for bound in self.binds.iter().filter(|bound| bound.kind == bind::ASSIGN && (bound.scope < 0 || open[bound.scope as usize])) {
            if self.heads(bound) { found.push((bound.line, -1, self.toks(bound.line).1[0], bound.scope)); }
        }
        found.sort_by_key(|item| (item.0, item.1 < 0));
        let mut symbols = Symbols { entries: vec![], of: vec![-1; self.defs.len()], index: HashMap::new(), hints };
        for (line, def, tok, scope) in found {
            let (chunk, _) = row(line);
            let name = word(&chunk.text, &tok).to_string();
            // (A definition without a name has no entry: what is in it hangs on the one around.)
            let parent = std::iter::successors(Some(scope), |&at| (at >= 0).then(|| self.defs[at as usize].parent)).take_while(|&at| at >= 0)
                .map(|at| symbols.of[at as usize]).find(|&entry| entry >= 0).unwrap_or(-1);
            let dotted = if parent >= 0 { format!("{}.{name}", symbols.entries[parent as usize].dotted) } else { name.clone() };
            let (kind, end) = if def < 0 {
                if symbols.entries.iter().rev().take(64).any(|entry| entry.dotted == dotted) { continue; }
                (2, line)
            } else {
                symbols.of[def as usize] = symbols.entries.len() as i32;
                let last = self.defs[def as usize].last;
                let closer = (last + 1..lines).find(|&below| code(below));
                (!self.defs[def as usize].class as u8, closer.map_or(tail.max(line), |closer| closer - 1))
            };
            let mut tint = if symbols.hinted(line.saturating_sub(64), line + if kind == 2 { 0 } else { 40 }) { self.tint(line, kind != 2, lines) } else { vec![] };
            if tint.is_empty() && kind == 0 && symbols.hinted(line + 1, end) {
                // A class's own `tint = (..)` or `tint: type = (..)`, wherever its body has it (what the
                // editor showed for a class before; the roster's resolver stops at the first method).
                let inset = (line + 1..=end).map(|below| text(&self.chunks, &self.line0, below)).find(|text| !text.trim().is_empty()).map(|text| text.len() - text.trim_start().len());
                tint = (line + 1..=end).map(|below| text(&self.chunks, &self.line0, below))
                    .filter(|text| Some(text.len() - text.trim_start().len()) == inset && text.trim_start().strip_prefix("tint").is_some_and(|rest| rest.trim_start().starts_with([':', '='])))
                    .find_map(|text| {
                        let value = text[text.find('=')? + 1..].trim();
                        let after = value.strip_prefix('(').and_then(|rest| rest.find(')').map(|close| rest[close + 1..].trim()))?;
                        (after.is_empty() || after.starts_with('#')).then(|| tint_in(&format!("tint={value}"))).flatten()
                    }).unwrap_or_default();
            }
            symbols.index.entry((parent, name.clone())).or_insert(symbols.entries.len());
            symbols.entries.push(Entry { dotted, name, kind, line, end, indent: lead(line), column: column(line, &tok), parent, shown: shown(line), tint });
        }
        symbols
    }

    /// Whether an assignment's name stands first on its line, as `name =` or `name: type =`.
    fn heads(&self, bound: &Binding) -> bool {
        let (chunk, toks) = self.toks(bound.line);
        let at = self.line0[..self.chunks.len()].partition_point(|&start| start <= bound.line) - 1;
        let is = |index: usize, op: &str| toks.get(index).is_some_and(|tok| tok.kind == scan::OP && word(&chunk.text, tok) == op);
        toks.first().is_some_and(|tok| tok.start == bound.start) && !self.chunk(at).continued[(bound.line - self.line0[at]) as usize]
            && (is(1, "=") || (is(1, ":") && (2..toks.len()).any(|index| is(index, "="))))
    }

    /// The tint the source states for the entry on `line` (a definition's
    /// or a variable's), as `symbol_roster._scan_tint` reads it: a
    /// `# [tint=(r, g, b)]` comment at the line's end or in the comments
    /// right above, a `tint=(..)` argument of a decorator above a
    /// definition, a `tint = (..)` in the first lines of its body. Empty: none.
    fn tint(&self, line: u32, definition: bool, lines: u32) -> Vec<f64> {
        let at = |line: u32| text(&self.chunks, &self.line0, line);
        let heads = |text: &str| ["class", "def"].iter().any(|word| text.trim_start().strip_prefix(word).is_some_and(|rest| rest.starts_with(char::is_whitespace)));
        // A comment that is all one `[key=value, ..]` list.
        let stated = |comments: &[&str]| {
            let body = comments.iter().map(|comment| comment.trim().trim_start_matches('#').trim()).collect::<Vec<_>>().join(" ");
            if body.starts_with('[') && body.ends_with(']') { tint_in(&body) } else { None }
        };
        let own = at(line);
        if let Some(found) = own.find('#').and_then(|hash| stated(&[&own[hash..]])) { return found; }
        let (mut run, mut above) = (vec![], line as i64 - 1);
        while above >= 0 {
            let text = at(above as u32).trim();
            if text.starts_with('#') {
                run.push(text);
                above -= 1;
            } else if text.starts_with('@') {
                if !definition { break; }
                if let Some(found) = tint_in(text) { return found; }
                above -= 1;
            } else if heads(text) {
                break;
            } else {
                // Maybe a decorator's further line: its first is a few lines up, with nothing blank between.
                let mut first = above - 1;
                while first >= 0 && first >= above - 20 && !at(first as u32).trim().starts_with('@') {
                    if at(first as u32).trim().is_empty() || heads(at(first as u32)) {
                        first = -1;
                        break;
                    }
                    first -= 1;
                }
                if first < 0 || !at(first as u32).trim().starts_with('@') || !definition { break; }
                let joined = (first..=above).map(|line| at(line as u32).trim()).collect::<Vec<_>>().join(" ");
                if let Some(found) = tint_in(&joined) { return found; }
                above = first - 1;
            }
        }
        run.reverse();
        if let Some(found) = (0..run.len()).find_map(|from| stated(&run[from..])) { return found; }
        if definition {
            let indent = |text: &str| text.len() - text.trim_start().len();
            for below in line + 1..(line + 40).min(lines) {
                let text = at(below);
                if text.trim().is_empty() { continue; }
                if indent(text) <= indent(own) { break; }
                // `tint = (..)` and nothing after it but a comment.
                let value = text.trim_start().strip_prefix("tint").and_then(|rest| rest.trim_start().strip_prefix('=')).map(str::trim_start);
                if let Some(close) = value.filter(|value| value.starts_with('(')).and_then(|value| value.find(')').map(|close| &value[close + 1..])) {
                    if close.trim().is_empty() || close.trim_start().starts_with('#') { return tint_in(text.trim_start()).unwrap_or_default(); }
                }
                if heads(text) { break; }
            }
        }
        vec![]
    }

    /// The names used that are symbols of this text, part by part: for
    /// each reference the entries its dotted parts resolve to, with where
    /// each part stands (line, column, characters). The first part is the
    /// name as the line sees it (a local or an import of that name hides
    /// the symbol); each further part is an entry in the one before.
    fn used<'a>(&'a self, symbols: &'a Symbols, first: u32, last: u32) -> impl Iterator<Item = (usize, u32, u32, u32)> + 'a {
        let mut sight = Sight { line: u32::MAX, scopes: vec![] };
        self.rows(first, last).flat_map(move |(at, local, row)| {
            let (bound, text) = (self.chunk(at), &self.chunks[at].text);
            let from = bound.refs.partition_point(|used| (used.line as usize) < local);
            let mut found = vec![];
            for used in bound.refs[from..].iter().take_while(|used| used.line as usize == local) {
                let chain = &text[used.start as usize..used.start as usize + used.len as usize];
                if chain.contains(char::is_whitespace) { continue; }
                let (mut entry, mut offset) = (None, 0);
                for (index, part) in chain.split('.').enumerate() {
                    entry = if index == 0 {
                        let scope = sight.at(self, row).iter().copied().find(|&scope| self.bound.contains_key(&mix(hash(part), scope as u64)));
                        scope.and_then(|scope| symbols.index.get(&(if scope < 0 { -1 } else { symbols.of[scope as usize] }, part.to_string())).copied()
                            .filter(|_| scope < 0 || symbols.of[scope as usize] >= 0))
                    } else {
                        entry.and_then(|entry: usize| symbols.index.get(&(entry as i32, part.to_string())).copied())
                    };
                    let Some(entry) = entry else { break };
                    let start = used.start + offset as u32;
                    found.push((entry, row, self.chars(at, local, start), part.chars().count() as u32));
                    offset += part.len() + 1;
                }
            }
            found
        })
    }

    /// What the definition tints paint, in line order: (line, column,
    /// characters, red, green, blue). A tinted class or function gives one
    /// row for its first line (0 characters: the whole definition is
    /// tinted from there), and every use of a tinted symbol of this text
    /// gives one over the name. So does a function's variable whose
    /// assignment states a tint (no entry of the table: it is local),
    /// wherever the function and those inside it use it.
    pub fn tints(&self) -> Vec<(u32, u32, u32, f64, f64, f64)> {
        let symbols = self.symbols();
        let colour = |entry: &Entry| (entry.tint.len() >= 3).then(|| (entry.tint[0], entry.tint[1], entry.tint[2]));
        if symbols.hints.is_empty() { return vec![]; }
        let lines = *self.line0.last().unwrap_or(&0);
        let mut found: Vec<(u32, u32, u32, f64, f64, f64)> = symbols.entries.iter().filter(|entry| entry.kind != 2)
            .filter_map(|entry| colour(entry).map(|(red, green, blue)| (entry.line, 0, 0, red, green, blue))).collect();
        if found.iter().len() > 0 || symbols.entries.iter().any(|entry| colour(entry).is_some()) {
            found.extend(self.used(&symbols, 0, u32::MAX - 1).filter_map(|(entry, line, column, chars)| {
                colour(&symbols.entries[entry]).map(|(red, green, blue)| (line, column, chars, red, green, blue))
            }));
        }
        // The variables of functions: (function, name) onto the tint its first assignment states.
        let mut locals: HashMap<(i32, u64), (f64, f64, f64)> = HashMap::new();
        for bound in self.binds.iter().filter(|bound| bound.kind == bind::ASSIGN && bound.scope >= 0 && !self.defs[bound.scope as usize].class) {
            if symbols.hinted(bound.line.saturating_sub(64), bound.line) && self.heads(bound) && !locals.contains_key(&(bound.scope, bound.hash)) {
                let tint = self.tint(bound.line, false, lines);
                if tint.len() >= 3 { locals.insert((bound.scope, bound.hash), (tint[0], tint[1], tint[2])); }
            }
        }
        if !locals.is_empty() {
            let mut sight = Sight { line: u32::MAX, scopes: vec![] };
            for (at, chunk) in self.chunks.iter().enumerate() {
                for used in &self.chunk(at).refs {
                    let chain = &chunk.text[used.start as usize..used.start as usize + used.len as usize];
                    let name = chain.split(|c: char| c == '.' || c.is_whitespace()).next().unwrap_or(chain);
                    let (row, key) = (self.line0[at] + used.line, hash(name));
                    let scope = sight.at(self, row).iter().copied().find(|&scope| self.bound.contains_key(&mix(key, scope as u64)));
                    if let Some(&(red, green, blue)) = scope.and_then(|scope| locals.get(&(scope, key))) {
                        found.push((row, self.chars(at, used.line as usize, used.start), name.chars().count() as u32, red, green, blue));
                    }
                }
            }
        }
        found.sort_by(|a, b| (a.0, a.1, a.2).cmp(&(b.0, b.1, b.2)));
        found
    }

    /// What the name at `column` of `line` leads to: (it is a definition,
    /// places as (line, column, characters)). On a use of a symbol of this
    /// text: where that is defined. On a definition's name: its uses.
    pub fn lookup(&self, line: u32, column: u32) -> Option<(bool, Vec<(u32, u32, u32)>)> {
        let symbols = self.symbols();
        let on = |start: u32, chars: u32| start <= column && column <= start + chars;
        if let Some(at) = symbols.entries.iter().position(|entry| entry.line == line && on(entry.column, entry.name.chars().count() as u32)) {
            let uses = self.used(&symbols, 0, u32::MAX - 1).filter(|used| used.0 == at && (used.1, used.2) != (line, symbols.entries[at].column));
            return Some((true, uses.map(|(_, line, column, chars)| (line, column, chars)).collect()));
        }
        let (entry, ..) = self.used(&symbols, line, line).find(|used| on(used.2, used.3))?;
        let entry = &symbols.entries[entry];
        Some((false, vec![(entry.line, entry.column, entry.name.chars().count() as u32)]))
    }

    /// The import statements of the table, wherever they stand (see `table`).
    #[allow(clippy::type_complexity)]
    fn imports(&self) -> (Vec<(String, String, Option<String>)>, Vec<String>) {
        // Import statements, wherever they stand.
        let (mut imports, mut stars): (Vec<(String, String, Option<String>)>, Vec<String>) = (vec![], vec![]);
        let mut put = |alias: &str, module: &str, name: Option<&str>, soft: bool| {
            match imports.iter_mut().find(|known| known.0 == alias) {
                Some(known) if !soft => (known.1, known.2) = (module.to_string(), name.map(str::to_string)),
                Some(_) => {}
                None => imports.push((alias.to_string(), module.to_string(), name.map(str::to_string))),
            }
        };
        for (at, chunk) in self.chunks.iter().enumerate() {
            let (scan, strings, bound) = (chunk.scan(), chunk.strings(self.entries[at]), self.chunk(at));
            for (local, line) in scan.lines.iter().enumerate() {
                if line.flags & scan::IMPORT == 0 || strings.lines[local] & 1 != 0 || bound.continued[local] { continue; }
                // The statement's words, over the lines it goes on, without brackets and comments.
                let first = self.line0[at] + local as u32;
                let mut words: Vec<(u8, &str)> = vec![];
                for (at, local, row) in self.rows(first, first + REACH) {
                    if row > first && !self.chunk(at).continued[local] { break; }
                    let chunk = &self.chunks[at];
                    words.extend(chunk.strings(self.entries[at]).on(local).iter().filter(|tok| tok.kind != scan::NOTE)
                        .map(|tok| (tok.kind, word(&chunk.text, tok))).filter(|tok| !matches!(tok.1, "(" | ")" | "\\")));
                }
                let end = words.iter().position(|tok| tok.1 == ";").unwrap_or(words.len());
                let words = &words[..end];
                let from = words.first().is_some_and(|tok| tok.1 == "from");
                let split = if from { words.iter().position(|tok| tok.0 == scan::KEYWORD && tok.1 == "import") } else { Some(0) };
                let Some(split) = split else { continue };
                let module: String = words[1..split.max(1)].iter().map(|tok| tok.1).collect();
                for part in words[split + 1..].split(|tok| tok.1 == ",") {
                    let alias = part.iter().position(|tok| tok.0 == scan::KEYWORD && tok.1 == "as");
                    let (path, rest) = part.split_at(alias.unwrap_or(part.len()));
                    let named = |toks: &[(u8, &str)]| toks.len() == 1 && toks[0].0 == scan::NAME;
                    let alias = rest.get(1..).filter(|rest| named(rest)).map(|rest| rest[0].1);
                    if from {
                        if path.len() == 1 && path[0].1 == "*" {
                            stars.push(module.clone());
                        } else if named(path) && (rest.is_empty() || alias.is_some()) && !module.is_empty() {
                            put(alias.unwrap_or(path[0].1), &module, Some(path[0].1), false);
                        }
                    } else if let Some(top) = path.first().filter(|tok| tok.0 == scan::NAME) {
                        let dotted: String = path.iter().map(|tok| tok.1).collect();
                        match alias {
                            Some(alias) => put(alias, &dotted, None, false),
                            None if rest.is_empty() => put(top.1, top.1, None, true),
                            None => {}
                        }
                    }
                }
            }
        }
        (imports, stars)
    }

    /// The table as the project roster keeps it (`roster.rs`): the entries
    /// and imports of `table`, and with `uses` every dotted chain the text
    /// uses, each with what its first name is from where it stands: a
    /// symbol of this text (the first scope in sight that binds the name
    /// has an entry of it), a member of the class around (`self.name`,
    /// kept without the `self.`), a name an import binds, a name nothing
    /// binds, or a local (kept only when dotted: its members may be
    /// someone's). The names import statements bind are uses too. A name a
    /// function declares `global` is the module's.
    pub(crate) fn roster(&self, uses: bool) -> crate::roster::Table {
        use crate::roster::{Entry as Row, Table, Use, FREE, IMPORTED, LOCAL, MEMBER, SYMBOL};
        let Symbols { entries, of, .. } = self.symbols();
        let (imports, stars) = self.imports();
        let entries = entries.into_iter().map(|entry| Row {
            dotted: entry.dotted.into(), name: entry.name.into(), kind: entry.kind, line: entry.line, end: entry.end, indent: entry.indent,
            column: entry.column, parent: entry.parent, shown: entry.shown.into(), tint: (entry.tint.len() >= 3).then(|| [entry.tint[0], entry.tint[1], entry.tint[2]]),
        }).collect();
        let mut table = Table::new(entries, imports, stars, *self.line0.last().unwrap_or(&0), self.version);
        if !uses { return table; }
        let entry = |scope: i32| if scope < 0 { -1 } else { of[scope as usize] };
        let global: HashSet<u64> = self.binds.iter().filter(|bound| bound.kind == bind::GLOBAL).map(|bound| mix(bound.hash, bound.scope as u64)).collect();
        let mut sight = Sight { line: u32::MAX, scopes: vec![] };
        for (at, chunk) in self.chunks.iter().enumerate() {
            for used in &self.chunk(at).refs {
                let chain = &chunk.text[used.start as usize..used.start as usize + used.len as usize];
                if chain.contains(char::is_whitespace) { continue; }
                let (row, name) = (self.line0[at] + used.line, chain.split('.').next().unwrap_or(chain));
                let key = hash(name);
                let scope = sight.at(self, row).iter().copied().find(|&scope| self.bound.contains_key(&mix(key, scope as u64)));
                let scope = if scope.is_some_and(|scope| global.contains(&mix(key, scope as u64))) { self.bound.contains_key(&mix(key, -1i32 as u64)).then_some(-1) } else { scope };
                let (mut kind, mut first, mut skip) = (FREE, -1, 0);
                if let Some(scope) = scope {
                    let only = self.bound[&mix(key, scope as u64)];
                    let symbol = table.child(entry(scope), name).filter(|_| scope < 0 || of[scope as usize] >= 0);
                    (kind, first) = match symbol {
                        Some(symbol) => (SYMBOL, symbol as i32),
                        None if only == BROUGHT || self.binds.get(only as usize).is_some_and(|found| found.kind == bind::IMPORT) => (IMPORTED, -1),
                        None => (LOCAL, -1),
                    };
                    // `self.name` in a method: a member of the class the method is in.
                    let class = std::iter::successors(Some(scope), |&at| (at >= 0).then(|| self.defs[at as usize].parent))
                        .take_while(|&at| at >= 0).find(|&at| self.defs[at as usize].class).map(|at| of[at as usize]);
                    if let (LOCAL, "self" | "cls", Some(class), true) = (kind, name, class.filter(|&class| class >= 0), chain.len() > name.len()) {
                        (kind, first, skip) = (MEMBER, class, name.len() + 1);
                    }
                }
                if kind == LOCAL && !chain.contains('.') { continue; }
                let column = self.chars(at, used.line as usize, used.start) + skip as u32;
                table.uses.push(Use::new(&mut table.chains, row, column, &chain[skip..], kind, first));
            }
        }
        for bound in self.binds.iter().filter(|bound| bound.kind == bind::IMPORT) {
            table.uses.push(Use::new(&mut table.chains, bound.line, self.column(bound), self.word(bound), IMPORTED, -1));
        }
        table.seal();
        table
    }

    /// Where the code of lines `first ..= last` stands, for what is drawn
    /// over a line (the live-value overlays): (line, column its code starts
    /// at, column it ends at, the right-hand side as (start, end) when an
    /// assignment operator stands on the line outside brackets). A comment
    /// is no code; lines without code are left out.
    #[allow(clippy::type_complexity)]
    pub fn spans(&self, first: u32, last: u32) -> Vec<(u32, u32, u32, Option<(u32, u32)>)> {
        const ASSIGNS: [&str; 15] = ["=", "+=", "-=", "*=", "/=", "//=", "%=", "**=", ">>=", "<<=", "&=", "|=", "^=", "@=", ":="];
        self.rows(first, last).filter_map(|(at, local, row)| {
            let chunk = &self.chunks[at];
            let toks: Vec<&Tok> = chunk.strings(self.entries[at]).on(local).iter().filter(|tok| tok.kind != scan::NOTE).collect();
            let column = |tok: &Tok| self.chars(at, local, tok.start);
            let end = toks.last().map(|tok| self.chars(at, local, tok.start + tok.len as u32))?;
            let mut depth = 0;
            let assign = toks.iter().position(|tok| {
                let text = word(&chunk.text, tok);
                depth += match (tok.kind, text) {
                    (scan::OP, "(" | "[" | "{") => 1,
                    (scan::OP, ")" | "]" | "}") => -1,
                    _ => 0,
                };
                depth == 0 && matches!(tok.kind, scan::OP | scan::OPS) && ASSIGNS.contains(&text)
            });
            let rhs = assign.and_then(|assign| toks.get(assign + 1)).map(|tok| (column(tok), end));
            Some((row, column(toks[0]), end, rhs))
        }).collect()
    }

    /// Where the name `label` stands on `line` as (column, end column): its
    /// first use as a name there; of a dotted label the last part, right
    /// after the parts before it. None when the line does not name it.
    pub fn label(&self, line: u32, label: &str) -> Option<(u32, u32)> {
        let (at, local, _) = self.rows(line, line).next()?;
        let chunk = &self.chunks[at];
        let toks: Vec<&Tok> = chunk.strings(self.entries[at]).on(local).iter().filter(|tok| tok.kind != scan::NOTE).collect();
        let parts: Vec<&str> = label.split('.').collect();
        let fits = |from: usize| parts.iter().enumerate().all(|(index, part)| {
            toks.get(from + 2 * index).is_some_and(|tok| tok.kind == scan::NAME && word(&chunk.text, tok) == *part)
                && (index == 0 || toks.get(from + 2 * index - 1).is_some_and(|tok| word(&chunk.text, tok) == "."))
        });
        let last = toks[(0..toks.len()).find(|&from| fits(from))? + 2 * (parts.len() - 1)];
        Some((self.chars(at, local, last.start), self.chars(at, local, last.start + last.len as u32)))
    }

    /// Where the calls that start on lines `first ..= last` close: (line,
    /// column, last line, column after the closing bracket), for those that
    /// close within `REACH` lines.
    pub fn closes(&self, first: u32, last: u32) -> Vec<(u32, u32, u32, u32)> {
        let mut found = vec![];
        for (at, local, line) in self.rows(first, last) {
            let bound = self.chunk(at);
            let from = bound.calls.partition_point(|call| (call.line as usize) < local);
            for call in bound.calls[from..].iter().take_while(|call| call.line as usize == local) {
                if let Some(args) = self.arguments(line, call.paren) {
                    found.push((line, self.chars(at, local, call.start), args.last, args.end));
                }
            }
        }
        found
    }

    /// The statement table in line order, with how far down the sites up to
    /// each one reach. Made once per reading, on whichever thread first asks:
    /// the reading thread when the request named site keys (`places`).
    fn sited(&self) -> &(Vec<Site>, Vec<u32>) {
        self.sited.get_or_init(|| {
            let mut sites = self.sites();
            sites.sort_by_key(|site| (site.0, site.1));
            let mut reach = 0;
            let reaches = sites.iter().map(|site| { reach = reach.max(site.2); reach }).collect();
            (sites, reaches)
        })
    }

    /// (first line, key path) of the sites that cover `line`, in source
    /// order: what a breakpoint on that line is kept by. Nothing unless the
    /// reading was asked for with site keys (the table is never made here).
    pub fn site(&self, line: u32) -> Vec<(u32, Vec<String>)> {
        let Some((sites, reaches)) = self.sited.get() else { return vec![] };
        let from = reaches.partition_point(|&reach| reach < line);
        sites[from..].iter().take_while(|site| site.0 <= line).filter(|site| site.2 >= line)
            .map(|site| (site.0, site.5.clone())).collect()
    }

    /// The first line of each site named by `keys`; None where the text has none.
    pub fn places(&self, keys: &[Vec<String>]) -> Vec<Option<u32>> {
        let wanted: HashMap<&[String], usize> = keys.iter().enumerate().map(|(at, key)| (key.as_slice(), at)).collect();
        let mut found = vec![None; keys.len()];
        for site in &self.sited().0 {
            if let Some(&at) = wanted.get(site.5.as_slice()) { found[at].get_or_insert(site.0); }
        }
        found
    }

    /// The statement table: every statement and every header of a compound
    /// one as (line, column, last line, end column, kind, key path), in
    /// text order; the source sites of `melty_scan.SourceSiteIndex` (the
    /// oracle of `tests/test_source.py`). Columns are characters; the end is
    /// exclusive. A compound statement is its header up to the `:`; what
    /// follows on the line, and statements parted by `;`, are sites of their
    /// own; a decorator is one from after its `@`. Kinds are the oracle's:
    /// Import, ImportFrom, Assign, AnnAssign, Expr (an augmented assignment
    /// too), Opaque (return, raise, pass, del .. and the headers of while,
    /// with, match), If, For, AsyncFor, Try, FunctionDef, AsyncFunctionDef,
    /// ClassDef, else, except, finally, decorator. The key path names the
    /// site from the module down (`Sites::key`): what a breakpoint is kept by.
    pub fn sites(&self) -> Vec<Site> {
        let mut sites = Sites { facts: self, found: vec![], open: vec![Body { indent: -1, ..Body::default() }], decorators: vec![], tree: None };
        self.pass(&mut sites, 0, u32::MAX);
        sites.found
    }

    /// The text as a tree of keys and values (`tree.rs`), made once per
    /// reading on whichever thread first asks: the reading thread when the
    /// request asked for it. `before`: the tree of an earlier reading,
    /// whose parts are taken for the statements that did not change.
    pub fn grown(&self, before: Option<&crate::tree::Tree>) -> &Arc<crate::tree::Tree> {
        self.grown.get_or_init(|| Arc::new(crate::tree::Tree::grow(self, before)))
    }

    /// The tree, when the reading was asked for with one (it is never made here).
    pub fn tree(&self) -> Option<&Arc<crate::tree::Tree>> {
        self.grown.get()
    }

    /// The top-level statements as `tops` gives them, as (first line, last
    /// line, key, the chunk it starts in). The key is the same while its
    /// tokens are: the same text in the same chunks, entered in the same
    /// string state. What a tree part is kept by.
    pub(crate) fn units(&self) -> Vec<(u32, u32, u64, u64)> {
        self.topped(true)
    }

    /// The tree part of the top-level statement on lines `first ..= last`:
    /// its statements read as the site table reads them, with the module's
    /// key counters standing at `seeds` (0 where none is given).
    pub(crate) fn unit(&self, first: u32, last: u32, seeds: &[(crate::tree::Counter, u32)]) -> crate::tree::Part {
        use crate::tree::Counter;
        let mut root = Body { indent: -1, ..Body::default() };
        for (counter, start) in seeds {
            match counter {
                Counter::Seen(key) => { root.seen.insert(key.clone(), *start); }
                Counter::Branch(at) => root.counts.borrow_mut().branches[*at as usize] = *start,
                Counter::Block(text) => { root.counts.borrow_mut().blocks.insert(text.clone(), *start); }
            }
        }
        let mut sites = Sites { facts: self, found: vec![], open: vec![root], decorators: vec![], tree: Some(crate::tree::Grower::new(first)) };
        self.pass(&mut sites, first, last);
        // What the module's counters were moved by: they number the keys of the statements after this one.
        let started = |counter: &Counter| seeds.iter().find(|seed| seed.0 == *counter).map_or(0, |seed| seed.1);
        let root = &sites.open[0];
        let counts = root.counts.borrow();
        let moved = root.seen.iter().map(|(key, count)| (Counter::Seen(key.clone()), *count))
            .chain(counts.branches.iter().enumerate().map(|(at, count)| (Counter::Branch(at as u8), *count)))
            .chain(counts.blocks.iter().map(|(text, count)| (Counter::Block(text.clone()), *count)));
        let touched = moved.map(|(counter, count)| { let start = started(&counter); (counter, start, count - start) })
            .filter(|(_, _, moved)| *moved > 0).collect();
        drop(counts);
        sites.tree.take().map(|tree| tree.done(touched)).unwrap_or_default()
    }

    /// The characters before `column` of `line`, in the text the facts were read from.
    pub fn offset(&self, line: u32, column: u32) -> usize {
        let (at, local, _) = self.rows(line, line).next().unwrap_or((0, 0, 0));
        let before: usize = self.chunks[..at].iter().map(|chunk| chunk.chars as usize).sum();
        let chunk = &self.chunks[at];
        let start = chunk.scan().lines.get(local).map_or(chunk.text.len(), |line| line.start as usize);
        before + if chunk.chars as usize == chunk.text.len() { start } else { chunk.text[..start].chars().count() } + column as usize
    }

    /// Feed `sites` the statements that start on lines `first ..= last`.
    fn pass<'a>(&'a self, sites: &mut Sites<'a>, first: u32, last: u32) {
        // The words of the statement being read: (line, column, end column, token kind, text).
        let mut words: Vec<Word> = vec![];
        let from = self.line0[..self.chunks.len()].partition_point(|&start| start <= first).saturating_sub(1);
        for (at, chunk) in self.chunks.iter().enumerate().skip(from) {
            if self.line0[at] > last { break; }
            let (scan, strings, bound) = (chunk.scan(), chunk.strings(self.entries[at]), self.chunk(at));
            for (local, line) in scan.lines.iter().enumerate() {
                let row = self.line0[at] + local as u32;
                if row < first { continue; }
                if row > last { break; }
                let inside = strings.lines[local] & 1 != 0;
                if !inside && !bound.continued[local] && line.flags & (scan::BLANK | scan::COMMENT) == 0 {
                    sites.statement(&words);
                    words.clear();
                }
                words.extend(strings.on(local).iter().filter(|tok| tok.kind != scan::NOTE).map(|tok| {
                    (row, self.chars(at, local, tok.start), self.chars(at, local, tok.start + tok.len as u32), tok.kind, word(&chunk.text, tok))
                }).filter(|word| word.4 != "\\"));
            }
        }
        sites.statement(&words);
    }

    /// The text from `column` of `line` to `end` of line `last` (columns in characters).
    pub(crate) fn slice(&self, line: u32, column: u32, last: u32, end: u32) -> String {
        (line..=last).map(|row| {
            let text = text(&self.chunks, &self.line0, row);
            let from = if row == line { column as usize } else { 0 };
            let upto = if row == last { end as usize } else { usize::MAX };
            text.chars().skip(from).take(upto.saturating_sub(from)).collect::<String>()
        }).collect::<Vec<_>>().join("\n")
    }

    /// The name a binding binds.
    pub fn word(&self, found: &Binding) -> &str {
        &self.chunks[found.chunk as usize].text[found.start as usize..found.start as usize + found.len as usize]
    }

    /// The column (in characters) a binding's name starts at.
    pub fn column(&self, found: &Binding) -> u32 {
        let (chunk, local) = place(&self.chunks, &self.line0, found.line);
        chunk.text[chunk.scan().lines[local].start as usize..found.start as usize].chars().count() as u32
    }

    /// The innermost definition whose body holds `line` (indented `indent`
    /// characters), or -1. A line right below a body, indented into it, is
    /// held too: the statement being typed at its end.
    pub fn inner(&self, line: u32, indent: u32) -> i32 {
        let lines = *self.line0.last().unwrap_or(&0);
        let mut at = self.defs.partition_point(|def| def.line < line) as i32 - 1;
        while at >= 0 {
            let def = &self.defs[at as usize];
            let trails = line > def.last && indent > def.indent && line - def.last <= 64 && (def.last + 1..line.min(lines)).all(|between| {
                let (chunk, local) = place(&self.chunks, &self.line0, between);
                chunk.scan().lines[local].flags & scan::BLANK != 0
            });
            if line <= def.last || trails { break; }
            at = def.parent;
        }
        at
    }

    /// The scopes whose names a line in definition `inner` sees, innermost
    /// first: the functions around it (a class only from its own body), then the module.
    fn chain(&self, inner: i32) -> Vec<i32> {
        let mut found = vec![inner];
        let mut at = inner;
        while at >= 0 {
            at = self.defs[at as usize].parent;
            if at < 0 || !self.defs[at as usize].class { found.push(at); }
        }
        found
    }

    /// The binding kind as completion shows it.
    fn tag(&self, found: &Binding) -> &'static str {
        match found.kind {
            bind::PARAM => "param",
            bind::DEF => "function",
            bind::CLASS => "class",
            bind::IMPORT => "module",
            _ if found.scope >= 0 && !self.defs[found.scope as usize].class => "local",
            _ => "var",
        }
    }

    /// The bindings a line sees, innermost scope first. In its own scope a
    /// plain name bound further down is not there yet.
    fn visible(&self, line: u32, indent: u32) -> impl Iterator<Item = &Binding> + '_ {
        let chain = self.chain(self.inner(line, indent));
        let own = chain[0];
        chain.into_iter().flat_map(move |scope| {
            let range = self.first[(scope + 1) as usize] as usize..self.first[(scope + 2) as usize] as usize;
            self.names[range].iter().take(LOOK).map(|&index| &self.binds[index as usize])
                .filter(move |found| scope != own || found.line <= line || found.kind <= bind::PARAM || found.kind == bind::IMPORT)
        })
    }

    /// (kind, line) of the binding `name` resolves to from `line`.
    pub fn bound(&self, name: &str, line: u32, indent: u32) -> Option<(&'static str, u32)> {
        self.visible(line, indent).find(|found| self.word(found) == name).map(|found| (bind::KINDS[found.kind as usize], found.line))
    }

    /// Completion candidates at `line` as (name, kind): the names bound in
    /// the scopes around it, innermost first, then the other names written
    /// near it in those scopes ("name"). Only names holding `prefix` (case blind).
    pub fn names(&self, line: u32, indent: u32, prefix: &str) -> Vec<(&str, &'static str)> {
        let needle = prefix.to_lowercase();
        let mut found: Vec<(&str, &'static str)> = vec![];
        for binding in self.visible(line, indent) {
            let name = self.word(binding);
            if holds(name, &needle) && !found.iter().any(|(other, _)| *other == name) { found.push((name, self.tag(binding))); }
            if found.len() >= OFFER { return found; }
        }
        let chain = self.chain(self.inner(line, indent));
        let lines = *self.line0.last().unwrap_or(&0);
        for near in line.saturating_sub(NEAR)..(line + NEAR).min(lines) {
            let at = self.line0[..self.chunks.len()].partition_point(|&start| start <= near) - 1;
            let (chunk, local) = (&self.chunks[at], (near - self.line0[at]) as usize);
            let (strings, scanned) = (chunk.strings(self.entries[at]), &chunk.scan().lines[local]);
            // Not a string's lines, not a header (its names are bindings), not another scope's.
            if strings.lines[local] & 1 != 0 || scanned.flags & (scan::DEF | scan::CLASS) != 0
                || !chain.contains(&self.inner(near, scanned.indent)) { continue; }
            let toks = strings.on(local);
            for (index, tok) in toks.iter().enumerate() {
                let name = &chunk.text[tok.start as usize..tok.start as usize + tok.len as usize];
                let member = index > 0 && toks[index - 1].kind == scan::OP && chunk.text.as_bytes()[toks[index - 1].start as usize] == b'.';
                if tok.kind == scan::NAME && !member && tok.len > 1 && holds(name, &needle) && !found.iter().any(|(other, _)| *other == name) {
                    found.push((name, "name"));
                    if found.len() >= OFFER { return found; }
                }
            }
        }
        found
    }

    /// (indent in spaces, it is a def or class) when range `at` is a scope or block.
    pub fn scope(&self, at: usize) -> Option<(u32, bool)> {
        (self.kinds[at] <= BLOCK).then(|| (self.indents[at], self.kinds[at] == SCOPE))
    }

    pub fn name(&self, at: usize) -> Name {
        self.spell(self.kinds[at], self.owners[at], self.heads[at], self.twins[at])
    }

    /// The names of the lone metadata comments.
    pub fn metas(&self) -> impl Iterator<Item = Name> + '_ {
        self.metas.iter().map(|&(line, twin)| self.spell(COMMENT, -1, line, twin))
    }

    fn spell(&self, kind: u8, owner: i32, head: u32, twin: u32) -> Name {
        let mut parts: Vec<Box<str>> = vec![KINDS[kind as usize].into()];
        if kind <= BLOCK {
            let mut around = owner;
            while around >= 0 {
                parts.insert(1, self.def(around as usize).into());
                around = self.defs[around as usize].parent;
            }
        }
        if kind == BLOCK {
            let (chunk, local) = place(&self.chunks, &self.line0, head);
            parts.push(KEYWORDS[chunk.scan().lines[local].kw as usize - 1].into());
        }
        if matches!(kind, BLOCK | STR | COMMENT) { parts.push(strip(text(&self.chunks, &self.line0, head)).into()); }
        Name { parts, twin }
    }

    /// The name of definition `at`.
    pub fn def(&self, at: usize) -> &str {
        let (chunk, local) = place(&self.chunks, &self.line0, self.defs[at].line);
        let names = &chunk.scan().names;
        names.binary_search_by_key(&(local as u32), |(line, _)| *line).map_or("?", |found| &names[found].1)
    }
}

/// The chunk holding `line`, and the line's index in it. `line0` may be
/// summed only as far as the line (the pass spells names while it runs).
fn place<'a>(chunks: &'a [Arc<Chunk>], line0: &[u32], line: u32) -> (&'a Chunk, usize) {
    let at = line0[..chunks.len().min(line0.len())].partition_point(|&start| start <= line) - 1;
    (&chunks[at], (line - line0[at]) as usize)
}

fn text<'a>(chunks: &'a [Arc<Chunk>], line0: &[u32], line: u32) -> &'a str {
    let (chunk, local) = place(chunks, line0, line);
    chunk.line(local)
}

/// Every occurrence of `term` (case blind), line by line and never overlapping.
pub fn find(chunks: &[Arc<Chunk>], term: &str) -> Vec<(u32, u32, u32)> {
    let mut hits = vec![];
    if term.is_empty() { return hits; }
    let needle = term.to_lowercase();
    let size = needle.chars().count() as u32;
    let plain = needle.is_ascii();
    let mut at = 0u32;
    for chunk in chunks {
        for line in &chunk.scan().lines {
            let row = &chunk.text[line.start as usize..line.end as usize];
            if row.len() >= needle.len() {
                if plain && row.is_ascii() {
                    let (row, needle) = (row.as_bytes(), needle.as_bytes());
                    let mut from = 0;
                    while from + needle.len() <= row.len() {
                        if row[from..from + needle.len()].eq_ignore_ascii_case(needle) {
                            hits.push((at, from as u32, from as u32 + size));
                            from += needle.len();
                        } else {
                            from += 1;
                        }
                    }
                } else {
                    let row = row.to_lowercase();
                    let mut from = 0;
                    while let Some(found) = row[from..].find(&needle) {
                        let column = row[..from + found].chars().count() as u32;
                        hits.push((at, column, column + size));
                        from += found + needle.len();
                    }
                }
            }
            at += 1;
        }
    }
    hits
}
