//! The project roster: the symbol table of every file of a project, and
//! the names across them.
//!
//! ```text
//! roster = Roster(wake)
//! roster.files(paths, roots)                 # the project's files, once: read cold on the roster's thread
//! roster.push(path, text, generation)        # a file's text changed (an unsaved edit)
//! source.join(roster, path)                  # an open Source: every reading it lands is its file's table
//! roster.tints(source, path)                 # (rows, needs): what a buffer washes for other files' tints
//! roster.lookup(source, path, line, column)  # use -> definition, definition -> uses, across files
//! roster.stale(needs)                        # after `wake()`: did what an answer hung on change
//! ```
//!
//! A file's table (`Table`) is plain rows: its entries and imports as
//! `Facts::table` has them, and every dotted chain it uses with what the
//! chain's first name is from where it stands (`Facts::roster`). Nothing
//! else of a file is kept: no text, no tokens. The tables are made on the
//! roster's thread, without the interpreter lock, from three things, the
//! newest of each: a reading an open `Source` landed (`hand`), a text Python
//! pushed, the file on disk. Python says which files there are and what
//! their unsaved text is; it never reads a table as a list.
//!
//! Across files there are three indexes, kept up per installed table: a
//! module's dotted name onto its file (`modules`), a module level name onto
//! the files that define it (`names`: the guess for a name nothing imports),
//! and a name onto the files that use it (`posts`: where the uses of a
//! definition are looked for; exact per identifier, where the Python
//! roster asked a trigram index for candidates and scanned their text).
//!
//! Every answer is read under the index's read lock and asks for no file
//! itself: what it lacked (a project file not read yet, a module outside
//! the project) is fetched after, then it is asked again (`Shared::answer`).
//! So ask off the render thread. What an answer hung on comes back as
//! `Needs`: the keys of the tables, guesses and modules it looked at, each
//! of which is stamped with the roster's generation when it changes
//! (`Index::moved`). To index something else per file: give `Table` the
//! rows (`Facts::roster`, `Table::seal`) and `Index::install` the diff.
use std::collections::{HashMap, HashSet, VecDeque};
use std::hash::BuildHasherDefault;
use std::path::MAIN_SEPARATOR as SEP;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Condvar, Mutex, RwLock, Weak};
use std::time::{Duration, Instant};

use pyo3::prelude::*;
use pyo3::types::PyString;

use crate::facts::{hash, mix, Facts, Keyed, Plain};
use crate::store::Store;
use crate::Source;

/// What the first name of a chain used is, from where it stands
/// (`Use::kind`): a symbol of its own file (`first`: the entry), a member of
/// the class around it (`self.name`, kept without `self.`; `first`: the
/// class), a name an import binds, a name nothing binds, a local.
pub const SYMBOL: u8 = 0;
pub const MEMBER: u8 = 1;
pub const IMPORTED: u8 = 2;
pub const FREE: u8 = 3;
pub const LOCAL: u8 = 4;

/// Levers: seconds without work before the thread ends; seconds between
/// two wakes while files keep arriving (the first indexing); the imports a
/// name is followed through (re-exports); how often an answer is asked
/// again after fetching what it lacked; the largest file read.
const IDLE: u64 = 20;
const PACE: f64 = 0.1;
const DEPTH: u32 = 4;
const ROUNDS: u32 = 6;
const LARGEST: u64 = 8 << 20;

/// What `Index::moved` keys are of: a file's table, a module level name, a module's dotted name.
const FILE: u64 = 0xf11e;
const NAME: u64 = 0x4a3e;
const MODULE: u64 = 0x30d1;
/// In an answer: the asking buffer's own table, when the roster's is of another reading.
const OWN: u32 = u32::MAX;
/// What is never a name to resolve (`symbol_roster._KEYWORDS`).
const RESERVED: [&str; 37] = [
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif", "else", "except",
    "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try",
    "while", "with", "yield", "self", "cls"];

/// Every roster's thread state, to end the threads when the interpreter exits.
static LIVE: Mutex<Vec<Weak<Shared>>> = Mutex::new(Vec::new());

/// One entry of a file's table, as `Facts::table` gives it; `kind` 0 class, 1 def, 2 var.
pub struct Entry {
    pub dotted: Box<str>,
    pub name: Box<str>,
    pub kind: u8,
    pub line: u32,
    pub end: u32,
    pub indent: u32,
    pub column: u32,
    pub parent: i32,
    pub shown: Box<str>,
    pub tint: Option<[f64; 3]>,
}

/// A dotted chain used: where it stands, and what its first name is (see `SYMBOL`).
pub struct Use {
    pub line: u32,
    pub column: u32,
    at: u32,
    len: u16,
    pub kind: u8,
    pub first: i32,
}

impl Use {
    pub fn new(chains: &mut Chains, line: u32, column: u32, chain: &str, kind: u8, first: i32) -> Use {
        Use { line, column, at: chains.put(chain), len: chain.len() as u16, kind, first }
    }
}

/// The chains a table's uses spell, each once.
#[derive(Default)]
pub struct Chains {
    text: String,
    seen: Keyed<u32>,
    /// Every name the chains hold, hashed.
    words: Vec<u64>,
}

impl Chains {
    fn put(&mut self, chain: &str) -> u32 {
        let (text, words) = (&mut self.text, &mut self.words);
        *self.seen.entry(hash(chain)).or_insert_with(|| {
            words.extend(chain.split('.').map(hash));
            text.push_str(chain);
            (text.len() - chain.len()) as u32
        })
    }
}

/// One file's table.
pub struct Table {
    pub entries: Vec<Entry>,
    /// A dotted name, hashed, onto the first entry of that name (as the Python roster: a class
    /// written twice is its first entry, with the members of both).
    index: Keyed<u32>,
    /// (alias, module as written, name or None), and the modules starred.
    pub imports: Vec<(String, String, Option<String>)>,
    pub stars: Vec<String>,
    /// In line order.
    pub uses: Vec<Use>,
    pub chains: Chains,
    pub lines: u32,
    /// The reading it was made from, (which, its version): (0, 0) for a text read here.
    from: (usize, u64),
    /// What other files see of it, hashed: names, kinds, tints, imports. Not lines.
    print: u64,
    /// Sorted hashes: its module level names, its other names, the names it uses.
    tops: Vec<u64>,
    inner: Vec<u64>,
    words: Vec<u64>,
}

impl Table {
    pub fn new(entries: Vec<Entry>, imports: Vec<(String, String, Option<String>)>, stars: Vec<String>, lines: u32, version: u64) -> Table {
        let mut table = Table { entries, index: Keyed::default(), imports, stars, uses: vec![], chains: Chains::default(), lines,
                                from: (0, version), print: 0x6d65_6c74_7900_0002, tops: vec![], inner: vec![], words: vec![] };
        for (at, entry) in table.entries.iter().enumerate() {
            table.index.entry(hash(&entry.dotted)).or_insert(at as u32);
            if entry.parent < 0 { table.tops.push(hash(&entry.name)); } else { table.inner.push(hash(&entry.name)); }
            table.print = mix(mix(table.print, hash(&entry.dotted)), entry.kind as u64);
            for part in entry.tint.iter().flatten() { table.print = mix(table.print, part.to_bits()); }
        }
        for (alias, module, name) in &table.imports {
            table.print = mix(mix(mix(table.print, hash(alias)), hash(module)), name.as_deref().map_or(1, hash));
        }
        for module in &table.stars { table.print = mix(table.print, !hash(module)); }
        table.tops.sort_unstable();
        table.inner.sort_unstable();
        table
    }

    /// After the uses were added: put them in order, list the names they hold.
    pub fn seal(&mut self) {
        self.uses.sort_unstable_by_key(|used| (used.line, used.column));
        self.uses.shrink_to_fit();
        self.chains.text.shrink_to_fit();
        self.chains.seen = Keyed::default();
        self.words = std::mem::take(&mut self.chains.words);
        self.words.sort_unstable();
        self.words.dedup();
        self.words.shrink_to_fit();
    }

    /// The entry `name` in entry `parent` (-1: the module).
    pub fn child(&self, parent: i32, name: &str) -> Option<u32> {
        let Some(around) = self.entries.get(parent as usize).filter(|_| parent >= 0) else {
            return self.index.get(&hash(name)).copied().filter(|&at| &*self.entries[at as usize].dotted == name);
        };
        // (The hash of "around.name", gone on from the hash of "around".)
        let key = [b"." as &[u8], name.as_bytes()].concat().iter().fold(hash(&around.dotted), |hash, &byte| (hash ^ byte as u64).wrapping_mul(0x100_0000_01b3));
        self.index.get(&key).copied().filter(|&at| {
            let found = &self.entries[at as usize];
            found.dotted.len() == around.dotted.len() + 1 + name.len() && found.dotted.starts_with(&*around.dotted) && found.dotted.ends_with(name)
        })
    }

    fn chain(&self, used: &Use) -> &str {
        &self.chains.text[used.at as usize..used.at as usize + used.len as usize]
    }

    /// The entries `parts` name one in the other, from entry `from` down:
    /// (entry, parts it took) for every prefix that is one.
    fn walk(&self, mut from: i32, parts: &[&str]) -> Vec<(u32, usize)> {
        let mut found = vec![];
        for (at, part) in parts.iter().enumerate() {
            let Some(entry) = self.child(from, part) else { break };
            found.push((entry, at + 1));
            from = entry as i32;
        }
        found
    }

    /// Bytes it holds, about.
    fn weight(&self) -> usize {
        let entries: usize = self.entries.iter().map(|entry| std::mem::size_of::<Entry>() + entry.dotted.len() + entry.name.len() + entry.shown.len()).sum();
        let imports: usize = self.imports.iter().map(|row| 72 + row.0.len() + row.1.len() + row.2.as_ref().map_or(0, String::len)).sum();
        entries + imports + self.index.capacity() * 16 + self.uses.capacity() * std::mem::size_of::<Use>() + self.chains.text.capacity()
            + (self.tops.capacity() + self.inner.capacity() + self.words.capacity()) * 8
    }
}

/// Read a text's table here: what the thread does for a cold or pushed file.
fn table(text: &str, uses: bool) -> Table {
    let mut table = Facts::read(Store::new(text).chunks, 0, false).roster(uses);
    table.from = (0, 0);
    table
}

/// A file's text from disk, line breaks as '\n'; empty when it cannot be read or is larger than `LARGEST`.
fn disk(path: &str) -> String {
    if std::fs::metadata(path).map_or(true, |meta| meta.len() > LARGEST) { return String::new(); }
    let text = std::fs::read(path).map_or(String::new(), |bytes| String::from_utf8_lossy(&bytes).into_owned());
    if text.contains('\r') { text.replace("\r\n", "\n").replace('\r', "\n") } else { text }
}

struct File {
    path: String,
    table: Option<Table>,
    /// The generation of the text Python last pushed.
    told: u64,
    /// The roster's generation when what other files see of its table last changed.
    generation: u64,
    /// An open Source's readings are its table: pushed texts and the disk wait.
    held: bool,
    /// One of the project's files: its names are indexed. Else a module found on disk for an import.
    inside: bool,
}

type Set = HashSet<u64, BuildHasherDefault<Plain>>;

#[derive(Default)]
struct Index {
    files: Vec<File>,
    ids: HashMap<String, u32>,
    /// The folders imports are looked up in, in order (the project's first).
    roots: Vec<String>,
    /// A module's dotted name onto (how early in `roots` it was found, its file).
    modules: HashMap<String, (u32, u32)>,
    /// The modules looked for on disk that are in no root.
    absent: HashSet<String>,
    /// A module level name onto the project files that define it, one per definition.
    names: Keyed<Vec<u32>>,
    /// Any other entry's name (a member) likewise.
    members: Keyed<Vec<u32>>,
    /// A name onto the project files whose chains hold it.
    posts: Keyed<Vec<u32>>,
    /// What an answer can hang on (`FILE`, `NAME`, `MODULE` keys), onto the generation it last changed at.
    moved: Keyed<u64>,
    generation: u64,
}

/// Go through two sorted lists: `tell(key, it came)` for what only one of them has.
fn differ(old: &[u64], new: &[u64], mut tell: impl FnMut(u64, bool)) {
    let (mut a, mut b) = (0, 0);
    while a < old.len() || b < new.len() {
        if b == new.len() || (a < old.len() && old[a] < new[b]) {
            tell(old[a], false);
            a += 1;
        } else if a == old.len() || new[b] < old[a] {
            tell(new[b], true);
            b += 1;
        } else {
            (a, b) = (a + 1, b + 1);
        }
    }
}

fn list(posts: &mut Keyed<Vec<u32>>, key: u64, file: u32, come: bool) {
    let files = posts.entry(key).or_default();
    match (come, files.iter().position(|&other| other == file)) {
        (true, _) => files.push(file),
        (false, Some(at)) => { files.swap_remove(at); }
        (false, None) => {}
    }
    if files.is_empty() { posts.remove(&key); }
}

impl Index {
    /// The file of `path`, added when it is new.
    fn file(&mut self, path: &str, inside: bool) -> u32 {
        if let Some(&id) = self.ids.get(path) {
            if inside && !self.files[id as usize].inside { self.enter(id); }
            return id;
        }
        let id = self.files.len() as u32;
        self.files.push(File { path: path.to_string(), table: None, told: 0, generation: 0, held: false, inside: false });
        self.ids.insert(path.to_string(), id);
        if inside { self.enter(id); }
        id
    }

    /// A file becomes one of the project's: its module names are known.
    /// (A table read while it was none has no uses: it is read again.)
    fn enter(&mut self, id: u32) {
        self.generation += 1;
        let file = &mut self.files[id as usize];
        (file.inside, file.table) = (true, None);
        self.moved.insert(mix(id as u64, FILE), self.generation);
        self.link(id);
    }

    /// Put a project file's module names in `modules`.
    fn link(&mut self, id: u32) {
        for (dotted, rank) in self.dotted(&self.files[id as usize].path) {
            self.moved.insert(mix(hash(&dotted), MODULE), self.generation);
            if self.modules.get(&dotted).map_or(true, |known| rank < known.0) { self.modules.insert(dotted, (rank, id)); }
        }
    }

    /// The module names `path` has under the roots, each with how early it would be found:
    /// by root, then a package's `__init__.py` before a `.py` before a `.pyi`.
    fn dotted(&self, path: &str) -> Vec<(String, u32)> {
        let mut found = vec![];
        for (at, root) in self.roots.iter().enumerate() {
            let Some(rest) = path.strip_prefix(root.as_str()).and_then(|rest| rest.strip_prefix(SEP)) else { continue };
            let (stem, kind) = match (rest.strip_suffix(".py"), rest.strip_suffix(".pyi")) {
                (Some(stem), _) => (stem, 1),
                (_, Some(stem)) => (stem, 2),
                _ => continue,
            };
            let mut parts: Vec<&str> = stem.split(SEP).collect();
            let kind = if parts.last() == Some(&"__init__") { parts.pop(); 0 } else { kind };
            if parts.is_empty() || parts.iter().any(|part| part.is_empty() || part.contains('.')) { continue; }
            found.push((parts.join("."), at as u32 * 3 + kind));
        }
        found
    }

    /// Put a file's table in place of the one it had. Returns whether other files see a difference.
    fn install(&mut self, id: u32, table: Table) -> bool {
        let old = self.files[id as usize].table.take();
        let changed = old.as_ref().map_or(true, |old| old.print != table.print);
        if changed { self.generation += 1; }
        if self.files[id as usize].inside {
            let none = (vec![], vec![], vec![]);
            let (tops, inner, words) = old.as_ref().map_or((&none.0, &none.1, &none.2), |old| (&old.tops, &old.inner, &old.words));
            let (names, moved, generation) = (&mut self.names, &mut self.moved, self.generation);
            differ(tops, &table.tops, |key, come| {
                list(names, key, id, come);
                moved.insert(mix(key, NAME), generation);
            });
            differ(inner, &table.inner, |key, come| list(&mut self.members, key, id, come));
            differ(words, &table.words, |key, come| list(&mut self.posts, key, id, come));
        }
        let file = &mut self.files[id as usize];
        if changed {
            file.generation = self.generation;
            self.moved.insert(mix(id as u64, FILE), self.generation);
        }
        file.table = Some(table);
        changed
    }

    /// A file is no longer the project's: its names leave the indexes.
    fn leave(&mut self, id: u32) {
        let empty = Table::new(vec![], vec![], vec![], 0, 0);
        self.install(id, empty);
        let file = &mut self.files[id as usize];
        (file.table, file.inside, file.held, file.told) = (None, false, false, 0);
        let path = file.path.clone();
        for (dotted, _) in self.dotted(&path) {
            if self.modules.get(&dotted).is_some_and(|known| known.1 == id) { self.modules.remove(&dotted); }
            self.moved.insert(mix(hash(&dotted), MODULE), self.generation);
        }
    }
}

/// What an answer lacked: a file's table, a module that is no project file.
enum Want {
    File(u32),
    Module(String),
}

/// (file, entry, parts of the chain it took).
type Hit = (u32, u32, usize);
/// A place: (path, line, column, characters, it is a guess).
type Place = (String, u32, u32, u32, bool);

/// One answer being read off the index.
struct Ask<'a> {
    index: &'a Index,
    /// The asking buffer's path and table, when the index has another reading of it.
    own: Option<(&'a str, &'a Table)>,
    needs: Set,
    wanted: Vec<Want>,
}

impl<'a> Ask<'a> {
    fn table(&mut self, file: u32) -> Option<&'a Table> {
        if file == OWN { return self.own.map(|own| own.1); }
        self.needs.insert(mix(file as u64, FILE));
        let found = self.index.files[file as usize].table.as_ref();
        if found.is_none() { self.wanted.push(Want::File(file)); }
        found
    }

    fn path(&self, file: u32) -> &'a str {
        if file == OWN { self.own.map_or("", |own| own.0) } else { &self.index.files[file as usize].path }
    }

    /// The file of module `dotted`.
    fn module(&mut self, dotted: &str) -> Option<u32> {
        self.needs.insert(mix(hash(dotted), MODULE));
        let found = self.index.modules.get(dotted).map(|known| known.1);
        if found.is_none() && !self.index.absent.contains(dotted) { self.wanted.push(Want::Module(dotted.to_string())); }
        found
    }

    /// A module as an import statement of `file` writes it, from the roots:
    /// leading dots count up from the file's package (the deepest root that holds it).
    fn absolute(&self, module: &str, file: u32) -> Option<String> {
        let dots = module.len() - module.trim_start_matches('.').len();
        if dots == 0 { return Some(module.to_string()); }
        let path = self.path(file);
        let folder = &path[..path.rfind(SEP)?];
        let root = self.index.roots.iter().filter(|root| folder.strip_prefix(root.as_str()).is_some_and(|rest| rest.starts_with(SEP))).max_by_key(|root| root.len())?;
        let package: Vec<&str> = folder[root.len() + 1..].split(SEP).collect();
        if dots > package.len() { return None; }
        let mut parts = package[..package.len() + 1 - dots].to_vec();
        if dots < module.len() { parts.push(&module[dots..]); }
        Some(parts.join("."))
    }

    fn walked(&mut self, file: u32, from: i32, parts: &[&str], taken: usize) -> Vec<Hit> {
        self.table(file).map_or(vec![], |table| table.walk(from, parts).into_iter().map(|(entry, count)| (file, entry, count + taken)).collect())
    }

    /// What `parts` name through the import of `file` that binds their first
    /// name, or None when no import does. `import a.b as z`: the longest
    /// module the parts spell, then its entries. `from a import b`: the
    /// entry `b` of `a`, or what `a` itself imports as `b` (a re-export, a
    /// package's `__init__`), or the module `a.b`.
    fn imported(&mut self, file: u32, table: &Table, parts: &[&str], depth: u32) -> Option<Vec<Hit>> {
        let (_, module, name) = table.imports.iter().find(|row| row.0 == parts[0])?;
        let Some(module) = self.absolute(module, file) else { return Some(vec![]) };
        let Some(name) = name else {
            for count in (1..=parts.len()).rev() {
                let dotted = std::iter::once(module.as_str()).chain(parts[1..count].iter().copied()).collect::<Vec<_>>().join(".");
                if let Some(other) = self.module(&dotted) {
                    // (All of the chain is a module: no symbol.)
                    return Some(if count == parts.len() { vec![] } else { self.walked(other, -1, &parts[count..], count) });
                }
            }
            return Some(vec![]);
        };
        let renamed: Vec<&str> = std::iter::once(name.as_str()).chain(parts[1..].iter().copied()).collect();
        let inner = self.module(&module);
        if let Some((other, found)) = inner.and_then(|other| Some((other, self.table(other)?))) {
            if found.child(-1, name).is_some() { return Some(self.walked(other, -1, &renamed, 0)); }
        }
        if parts.len() > 1 {
            if let Some(other) = self.module(&format!("{module}.{name}")) {
                let found = self.walked(other, -1, &parts[1..], 1);
                if !found.is_empty() { return Some(found); }
            }
        }
        if let (Some((other, found)), true) = (inner.and_then(|other| Some((other, self.table(other)?))), depth < DEPTH) {
            let passed = self.imported(other, found, &renamed, depth + 1).filter(|hits| !hits.is_empty());
            return Some(passed.unwrap_or_else(|| self.starred(other, found, &renamed, depth + 1)));
        }
        Some(vec![])
    }

    /// What `parts` name through the star imports of `file`: an entry of a
    /// starred module, or what that module imports under the name.
    fn starred(&mut self, file: u32, table: &Table, parts: &[&str], depth: u32) -> Vec<Hit> {
        for module in &table.stars {
            let Some(other) = self.absolute(module, file).and_then(|module| self.module(&module)) else { continue };
            let Some(found) = self.table(other) else { continue };
            if found.child(-1, parts[0]).is_some() { return self.walked(other, -1, parts, 0); }
            if depth < DEPTH {
                let passed = self.imported(other, found, parts, depth + 1).filter(|hits| !hits.is_empty()).unwrap_or_else(|| self.starred(other, found, parts, depth + 1));
                if !passed.is_empty() { return passed; }
            }
        }
        vec![]
    }

    /// The guess for a name nothing brings: the one module level definition of it in the project.
    fn guessed(&mut self, parts: &[&str]) -> Vec<Hit> {
        let key = hash(parts[0]);
        self.needs.insert(mix(key, NAME));
        match self.index.names.get(&key).map(Vec::as_slice) {
            Some(&[file]) => self.walked(file, -1, parts, 0),
            _ => vec![],
        }
    }

    /// What `parts` name seen from entry `scope` of `file` (a dotted name;
    /// None: the module), by the tables alone, as `symbol_roster.resolve`
    /// does: `self.x` in a class, the entries of the scopes around (a class
    /// only from its own body), the module's, its imports, its star
    /// imports, then with `guess` the one definition of the name anywhere.
    fn named(&mut self, file: u32, scope: Option<&str>, parts: &[&str], guess: bool) -> Vec<Hit> {
        let Some(table) = self.table(file) else { return vec![] };
        let own = |found: Vec<(u32, usize)>, skip: usize| found.into_iter().filter(|hit| hit.1 > skip).map(|(entry, count)| (file, entry, count - skip)).collect::<Vec<Hit>>();
        let scope = scope.map_or(-1, |scope| {
            let names: Vec<&str> = scope.split('.').collect();
            table.walk(-1, &names).last().filter(|hit| hit.1 == names.len()).map_or(-1, |hit| hit.0 as i32)
        });
        let around = |from: i32| std::iter::successors(Some(from).filter(|&at| at >= 0), |&at| Some(table.entries[at as usize].parent).filter(|&at| at >= 0));
        if matches!(parts[0], "self" | "cls") && scope >= 0 && parts.len() > 1 {
            let class = around(scope).find(|&at| table.entries[at as usize].kind == 0);
            return class.map_or(vec![], |class| table.walk(class, &parts[1..]).into_iter().map(|(entry, count)| (file, entry, count + 1)).collect());
        }
        if RESERVED.contains(&parts[0]) { return vec![]; }
        for at in around(scope).filter(|&at| at == scope || table.entries[at as usize].kind != 0) {
            if table.child(at, parts[0]).is_some() { return own(table.walk(at, parts), 0); }
        }
        if table.child(-1, parts[0]).is_some() { return own(table.walk(-1, parts), 0); }
        if let Some(found) = self.imported(file, table, parts, 0) { return found; }
        let found = self.starred(file, table, parts, 0);
        if found.is_empty() && guess { self.guessed(parts) } else { found }
    }

    /// What the parts of a chain used in `file` name: by what its first
    /// name is where it stands (`Use::kind`), which the file's own reading
    /// knew. A guess (the third value) when nothing brings the name.
    fn follow(&mut self, file: u32, table: &Table, used: &Use, parts: &[&str]) -> (Vec<Hit>, bool) {
        match used.kind {
            SYMBOL => (std::iter::once((file, used.first as u32, 1)).chain(table.walk(used.first, &parts[1..]).into_iter().map(|(entry, count)| (file, entry, count + 1))).collect(), false),
            MEMBER => (table.walk(used.first, parts).into_iter().map(|(entry, count)| (file, entry, count)).collect(), false),
            IMPORTED => (self.imported(file, table, parts, 0).unwrap_or_default(), false),
            FREE => {
                let found = self.starred(file, table, parts, 0);
                if found.is_empty() { (self.guessed(parts), true) } else { (found, false) }
            }
            _ => (vec![], false),
        }
    }

    /// The washes `file` takes from the tints other files state: (line,
    /// column, characters, red, green, blue, scale) over each part of a
    /// chain that names a tinted entry through an import, a star import or
    /// the guess. `scale` is the strength the Python roster gives a wash: 1
    /// for a use of the symbol itself.
    fn tints(&mut self, file: u32) -> Vec<(u32, u32, u32, f64, f64, f64, f64)> {
        let Some(table) = self.table(file) else { return vec![] };
        let mut rows = vec![];
        // (The same chain is used many times: followed once. Onto the parts that name a tinted entry, and its tint.)
        type Tinted = Vec<(usize, [f64; 3])>;
        let mut known: HashMap<(u32, u8), Tinted> = HashMap::new();
        for used in table.uses.iter().filter(|used| matches!(used.kind, IMPORTED | FREE)) {
            let chain = table.chain(used);
            let tinted = known.entry((used.at, used.kind)).or_insert_with(|| {
                let parts: Vec<&str> = chain.split('.').collect();
                let hits = self.follow(file, table, used, &parts).0;
                hits.into_iter().filter_map(|(other, entry, count)| Some((count, self.table(other)?.entries[entry as usize].tint?))).collect()
            });
            for &(count, [red, green, blue]) in tinted.iter() {
                let (column, chars) = part(chain, count - 1);
                rows.push((used.line, used.column + column, chars, red, green, blue, 1.));
            }
        }
        rows
    }

    /// The uses of entry `entry` of `file` in the project: every chain
    /// with the entry's name among its parts that leads to the entry from
    /// where it stands. A member also takes `anything.name` that leads
    /// nowhere while no other member of the project has the name (a guess).
    /// For the asking buffer's own table (`OWN`) the other files' chains
    /// lead to the table the index has of that file (`twin`): there the
    /// entry is the one of the same dotted name.
    fn uses(&mut self, file: u32, entry: u32) -> Vec<Place> {
        let Some(home) = self.table(file) else { return vec![] };
        let target = &home.entries[entry as usize];
        let (name, key) = (&*target.name, hash(&target.name));
        let lone = target.parent >= 0 && self.index.members.get(&key).map_or(0, Vec::len) <= 1;
        let twin = if file == OWN { self.index.ids.get(self.path(OWN)).copied() } else { None };
        let mut files: Vec<u32> = self.index.posts.get(&key).cloned().unwrap_or_default();
        files.retain(|&other| Some(other) != twin && other != file);
        files.push(file);
        files.sort_unstable_by_key(|&other| self.path(other));
        let mut places = vec![];
        for other in files {
            let Some(table) = self.table(other) else { continue };
            let mut known: HashMap<(u32, u8, i32, usize), (bool, bool)> = HashMap::new();
            for used in &table.uses {
                let chain = table.chain(used);
                if !chain.contains(name) { continue; }
                let parts: Vec<&str> = chain.split('.').collect();
                let Some(at) = parts.iter().position(|&part| part == name) else { continue };
                // (It leads to the entry, that is a guess.)
                let (leads, guess) = *known.entry((used.at, used.kind, used.first, at)).or_insert_with(|| {
                    let (hits, guess) = self.follow(other, table, used, &parts[..=at]);
                    match hits.last().filter(|hit| hit.2 == at + 1) {
                        Some(&(found, index, _)) if found == file => (index == entry, guess),
                        Some(&(found, index, _)) => (Some(found) == twin && self.table(found).is_some_and(|table| table.entries[index as usize].dotted == target.dotted), guess),
                        None => (lone && (at > 0 || used.kind == MEMBER), true),
                    }
                });
                let (column, chars) = part(chain, at);
                if leads && !(other == file && used.line == target.line) {
                    places.push((self.path(other).to_string(), used.line, used.column + column, chars, guess));
                }
            }
        }
        places
    }

    /// What the name at `column` of `line` of `file` leads to: (it is a
    /// definition, places). On an entry's name: its uses in the project.
    /// On a part of a chain used: the entry it names, wherever that is.
    fn lookup(&mut self, file: u32, line: u32, column: u32) -> Option<(bool, Vec<Place>)> {
        let table = self.table(file)?;
        let on = |start: u32, chars: u32| start <= column && column <= start + chars;
        if let Some(entry) = table.entries.iter().position(|entry| entry.line == line && on(entry.column, entry.name.chars().count() as u32)) {
            return Some((true, self.uses(file, entry as u32)));
        }
        let from = table.uses.partition_point(|used| used.line < line);
        for used in table.uses[from..].iter().take_while(|used| used.line == line) {
            let chain = table.chain(used);
            if !on(used.column, chain.chars().count() as u32) { continue; }
            let parts: Vec<&str> = chain.split('.').collect();
            let at = (0..parts.len()).find(|&at| { let (start, chars) = part(chain, at); on(used.column + start, chars) })?;
            let (hits, guess) = self.follow(file, table, used, &parts[..=at]);
            let &(other, entry, _) = hits.last().filter(|hit| hit.2 == at + 1)?;
            let found = &self.table(other)?.entries[entry as usize];
            return Some((false, vec![(self.path(other).to_string(), found.line, found.column, found.name.chars().count() as u32, guess)]));
        }
        None
    }
}

/// (column, characters) of part `at` of a dotted chain.
fn part(chain: &str, at: usize) -> (u32, u32) {
    let mut column = 0;
    for (index, part) in chain.split('.').enumerate() {
        let chars = part.chars().count() as u32;
        if index == at { return (column, chars); }
        column += chars + 1;
    }
    (column, 0)
}

/// What an answer hung on: ask `Roster.stale` whether any of it changed since.
#[pyclass(module = "meltygui.model._source_native")]
#[derive(Default)]
pub struct Needs {
    keys: Vec<u64>,
    stamp: u64,
}

#[derive(Default)]
struct Desk {
    /// The readings open sources landed and the texts pushed, by path: the newest of each waits.
    facts: HashMap<String, Arc<Facts>>,
    texts: HashMap<String, (String, u64)>,
    /// The files to read from disk, and those a source left.
    cold: VecDeque<u32>,
    left: Vec<String>,
    /// Called when tables changed, at most every `PACE` seconds while work waits.
    wake: Option<Py<PyAny>>,
    news: bool,
    working: bool,
    running: bool,
    closed: bool,
}

enum Job {
    Facts(String, Arc<Facts>),
    Text(String, String, u64),
    Left(String),
    Cold(u32),
}

impl Desk {
    fn next(&mut self) -> Option<Job> {
        if let Some(path) = self.facts.keys().next().cloned() {
            let facts = self.facts.remove(&path)?;
            return Some(Job::Facts(path, facts));
        }
        if let Some(path) = self.texts.keys().next().cloned() {
            let (text, told) = self.texts.remove(&path)?;
            return Some(Job::Text(path, text, told));
        }
        self.left.pop().map(Job::Left).or_else(|| self.cold.pop_front().map(Job::Cold))
    }
}

#[derive(Default)]
pub struct Shared {
    desk: Mutex<Desk>,
    bell: Condvar,
    index: RwLock<Index>,
}

/// A thread that died working must not leave `settle` and `close` waiting.
struct Shift<'a>(&'a Shared);

impl Drop for Shift<'_> {
    fn drop(&mut self) {
        if std::thread::panicking() {
            let mut desk = self.0.desk.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            (desk.running, desk.working) = (false, false);
            self.0.bell.notify_all();
        }
    }
}

impl Shared {
    /// A reading an open source landed is its file's table from now on.
    /// Costs the caller (a frame) one map entry: the table is made on the thread.
    pub fn hand(self: &Arc<Self>, path: &str, facts: &Arc<Facts>) {
        let mut desk = self.desk.lock().unwrap();
        desk.facts.insert(path.to_string(), facts.clone());
        self.ring(&mut desk);
    }

    /// Start the thread when there is none, and tell it there is work.
    fn ring(self: &Arc<Self>, desk: &mut Desk) {
        if !desk.running && !desk.closed && !crate::EXITING.load(Ordering::Relaxed) {
            desk.running = true;
            let shared = self.clone();
            std::thread::Builder::new().name("melty-roster".into()).spawn(move || shared.work()).unwrap();
        }
        self.bell.notify_all();
    }

    fn work(self: Arc<Self>) {
        let _shift = Shift(&self);
        let mut told = Instant::now();
        loop {
            let job = {
                let mut desk = self.desk.lock().unwrap();
                loop {
                    if desk.closed { (desk.running, desk.working) = (false, false); self.bell.notify_all(); return; }
                    // Tables changed: tell, once nothing waits or `PACE` after the last time.
                    if desk.news && (!desk.waits() || told.elapsed().as_secs_f64() >= PACE) {
                        (desk.news, desk.working) = (false, true);
                        break None;
                    }
                    if let Some(job) = desk.next() {
                        desk.working = true;
                        break Some(job);
                    }
                    desk.working = false;
                    self.bell.notify_all();
                    let (next, wait) = self.bell.wait_timeout(desk, Duration::from_secs(IDLE)).unwrap();
                    desk = next;
                    if wait.timed_out() && !desk.news && !desk.waits() { desk.running = false; self.bell.notify_all(); return; }
                }
            };
            match job {
                Some(job) => { if self.take(job) { self.desk.lock().unwrap().news = true; } }
                None => {
                    // Taking the interpreter lock while the interpreter exits aborts the process: `shut` ends this thread before.
                    if !crate::EXITING.load(Ordering::Relaxed) && unsafe { pyo3::ffi::Py_IsInitialized() } != 0 {
                        Python::with_gil(|py| {
                            let wake = self.desk.lock().unwrap().wake.as_ref().map(|wake| wake.clone_ref(py));
                            if let Some(Err(error)) = wake.map(|wake| wake.call0(py)) { error.print(py); }
                        });
                    }
                    told = Instant::now();
                }
            }
        }
    }

    /// Make one table and put it in. Returns whether other files see a difference.
    fn take(&self, job: Job) -> bool {
        match job {
            Job::Facts(path, facts) => {
                let from = (Arc::as_ptr(&facts) as usize, facts.version);
                let known = {
                    let index = self.index.read().unwrap();
                    index.ids.get(&path).and_then(|&id| index.files[id as usize].table.as_ref()).is_some_and(|table| table.from == from)
                };
                if known { return false; }
                let mut table = facts.roster(true);
                table.from = from;
                let mut index = self.index.write().unwrap();
                let id = index.file(&path, true);
                index.files[id as usize].held = true;
                index.install(id, table)
            }
            Job::Text(path, text, told) => {
                let table = table(&text, true);
                let mut index = self.index.write().unwrap();
                let id = index.file(&path, true);
                let file = &mut index.files[id as usize];
                if told < file.told { return false; }
                file.told = told;
                !file.held && index.install(id, table)
            }
            Job::Left(path) => {
                let table = table(&disk(&path), true);
                let mut index = self.index.write().unwrap();
                let Some(&id) = index.ids.get(&path) else { return false };
                let file = &mut index.files[id as usize];
                file.held = false;
                // (A text pushed since the source joined is still to come again: Python pushes it on leaving.)
                index.install(id, table)
            }
            Job::Cold(id) => self.fetch(vec![Want::File(id)]),
        }
    }

    /// Read what an answer lacked, here: files never read, from disk; modules
    /// that are no project files, looked for under the roots in order.
    fn fetch(&self, wanted: Vec<Want>) -> bool {
        let mut changed = false;
        for want in wanted {
            let id = match want {
                Want::File(id) => id,
                Want::Module(dotted) => {
                    let (roots, known) = {
                        let index = self.index.read().unwrap();
                        (index.roots.clone(), index.modules.contains_key(&dotted) || index.absent.contains(&dotted))
                    };
                    if known { continue; }
                    let inner = dotted.replace('.', std::path::MAIN_SEPARATOR_STR);
                    let found = roots.iter().enumerate().find_map(|(at, root)| {
                        [format!("{SEP}__init__.py"), ".py".to_string(), ".pyi".to_string()].iter().enumerate().find_map(|(kind, end)| {
                            let path = format!("{root}{SEP}{inner}{end}");
                            std::path::Path::new(&path).is_file().then(|| (std::fs::canonicalize(&path).map_or(path, |real| real.to_string_lossy().into_owned()), (at * 3 + kind) as u32))
                        })
                    });
                    let mut index = self.index.write().unwrap();
                    let Some((path, rank)) = found else {
                        index.absent.insert(dotted);
                        continue;
                    };
                    index.generation += 1;
                    let generation = index.generation;
                    index.moved.insert(mix(hash(&dotted), MODULE), generation);
                    let id = index.file(&path, false);
                    index.modules.insert(dotted, (rank, id));
                    id
                }
            };
            let (path, inside) = {
                let index = self.index.read().unwrap();
                let file = &index.files[id as usize];
                if file.table.is_some() || file.held { continue; }
                (file.path.clone(), file.inside)
            };
            let table = table(&disk(&path), inside);
            let mut index = self.index.write().unwrap();
            let file = &index.files[id as usize];
            if file.table.is_none() && !file.held { changed |= index.install(id, table); }
        }
        changed
    }

    /// Read an answer off the index: `run(ask, file)` for the file of
    /// `path`, or for the table of `facts` when the index has another
    /// reading of that buffer. What it lacked is fetched, then it is read
    /// again, up to `ROUNDS` times.
    fn answer<T: Default>(self: &Arc<Self>, path: &str, facts: Option<&Arc<Facts>>, run: impl Fn(&mut Ask, u32) -> T) -> (T, Needs) {
        let from = facts.map(|facts| (Arc::as_ptr(facts) as usize, facts.version));
        let mut private: Option<Table> = None;
        let mut round = 0;
        loop {
            let index = self.index.read().unwrap();
            let id = index.ids.get(path).copied();
            let current = from.map_or(true, |from| id.and_then(|id| index.files[id as usize].table.as_ref()).is_some_and(|table| table.from == from));
            if let (false, None, Some(facts)) = (current, &private, facts) {
                drop(index);
                private = Some(facts.roster(true));
                continue;
            }
            let file = if current { id } else { Some(OWN) };
            let Some(file) = file else { return (T::default(), Needs::default()) };
            let mut ask = Ask { index: &index, own: private.as_ref().filter(|_| !current).map(|table| (path, table)), needs: Set::default(), wanted: vec![] };
            let found = run(&mut ask, file);
            let (needs, wanted) = (Needs { keys: ask.needs.into_iter().collect(), stamp: index.generation }, ask.wanted);
            drop(index);
            round += 1;
            if wanted.is_empty() || round > ROUNDS { return (found, needs); }
            if self.fetch(wanted) {
                // Others may hang on what was fetched: the thread tells them.
                let mut desk = self.desk.lock().unwrap();
                desk.news = true;
                self.ring(&mut desk);
            }
        }
    }

    /// Tell the thread to end; what waits is dropped.
    fn end(&self) {
        let mut desk = self.desk.lock().unwrap();
        desk.closed = true;
        (desk.facts, desk.texts, desk.cold, desk.left) = Default::default();
        self.bell.notify_all();
    }

    fn join(&self) {
        let mut desk = self.desk.lock().unwrap();
        while desk.running { desk = self.bell.wait(desk).unwrap(); }
    }
}

impl Desk {
    fn waits(&self) -> bool {
        !(self.facts.is_empty() && self.texts.is_empty() && self.cold.is_empty() && self.left.is_empty())
    }
}

/// The roster of one project. See the module's text.
#[pyclass(module = "meltygui.model._source_native")]
pub struct Roster {
    pub(crate) shared: Arc<Shared>,
}

#[pymethods]
impl Roster {
    /// `wake()` is called from the roster's thread when tables changed.
    #[new]
    #[pyo3(signature = (wake=None))]
    fn new(wake: Option<Py<PyAny>>) -> Roster {
        let shared = Arc::<Shared>::default();
        shared.desk.lock().unwrap().wake = wake;
        let mut live = LIVE.lock().unwrap();
        live.retain(|other| other.strong_count() > 0);
        live.push(Arc::downgrade(&shared));
        Roster { shared }
    }

    /// The project's files, and the folders its imports are looked up in
    /// (in order: the project's own first). Files not read yet are read
    /// from disk on the thread; files no longer listed leave.
    fn files(&self, py: Python<'_>, paths: Vec<String>, roots: Vec<String>) {
        let shared = self.shared.clone();
        py.allow_threads(move || {
            let mut cold = vec![];
            {
                let mut index = shared.index.write().unwrap();
                let roots: Vec<String> = roots.into_iter().map(|root| root.trim_end_matches(SEP).to_string()).collect();
                if roots != index.roots {
                    // Every module name is from the roots: again for the files there are.
                    index.generation += 1;
                    let (generation, gone) = (index.generation, std::mem::take(&mut index.modules));
                    for dotted in gone.keys() { index.moved.insert(mix(hash(dotted), MODULE), generation); }
                    index.roots = roots;
                    for id in 0..index.files.len() as u32 {
                        if index.files[id as usize].inside { index.link(id); }
                    }
                }
                index.absent.clear();
                let listed: HashSet<&str> = paths.iter().map(String::as_str).collect();
                let gone: Vec<u32> = (0..index.files.len() as u32).filter(|&id| index.files[id as usize].inside && !listed.contains(index.files[id as usize].path.as_str())).collect();
                for id in gone { index.leave(id); }
                for path in &paths {
                    let id = index.file(path, true);
                    if index.files[id as usize].table.is_none() { cold.push(id); }
                }
            }
            let mut desk = shared.desk.lock().unwrap();
            desk.cold.extend(cold);
            desk.news = true;
            shared.ring(&mut desk);
        });
    }

    /// A file's text as it is now (an unsaved edit), at the caller's
    /// `generation` of it: an older one than the last told is dropped.
    /// The table is made on the thread. A file not listed joins the project.
    fn push(&self, path: String, text: &Bound<'_, PyString>, generation: u64) -> PyResult<()> {
        let bytes = crate::utf8(text)?;
        let text = crate::decoded(&bytes).to_string();
        let mut desk = self.shared.desk.lock().unwrap();
        if desk.texts.get(&path).map_or(true, |waiting| waiting.1 <= generation) { desk.texts.insert(path, (text, generation)); }
        self.shared.ring(&mut desk);
        Ok(())
    }

    /// No source is open on `path` any more: its table is the disk's again (push the unsaved text after).
    fn leave(&self, path: String) {
        let mut desk = self.shared.desk.lock().unwrap();
        desk.left.push(path);
        self.shared.ring(&mut desk);
    }

    /// The washes the landed reading of `source` (the file `path`) takes
    /// from tints other files state: (rows, needs). A row is (line, column,
    /// characters, red, green, blue, scale): what `Source.tints` rows are,
    /// and the strength of the wash. May read files: ask off the render thread.
    #[allow(clippy::type_complexity)]
    fn tints(&self, py: Python<'_>, source: PyRef<'_, Source>, path: String) -> (Vec<(u32, u32, u32, f64, f64, f64, f64)>, Needs) {
        let facts = source.landed.as_ref().map(|found| found.facts.clone());
        // (Not borrowed while the interpreter is let go: the writer may edit meanwhile.)
        drop(source);
        let shared = self.shared.clone();
        py.allow_threads(move || facts.map_or_else(Default::default, |facts| shared.answer(&path, Some(&facts), |ask, file| ask.tints(file))))
    }

    /// What the name at `column` of `line` leads to in the project, for
    /// the landed reading of `source` (the file `path`), or without a
    /// source for the file as the roster has it: (it is a definition,
    /// [(path, line, column, characters, it is a guess)]): the uses of the
    /// entry defined there, or the definition of the name used there. None
    /// elsewhere. May read files: ask off the render thread.
    #[pyo3(signature = (source, path, line, column))]
    fn lookup(&self, py: Python<'_>, source: Option<PyRef<'_, Source>>, path: String, line: u32, column: u32) -> Option<(bool, Vec<Place>)> {
        let facts = source.as_ref().and_then(|source| source.landed.as_ref().map(|found| found.facts.clone()));
        drop(source);
        let shared = self.shared.clone();
        py.allow_threads(move || shared.answer(&path, facts.as_ref(), |ask, file| ask.lookup(file, line, column)).0)
    }

    /// The entry the dotted `chain` names in file `path`, seen from the
    /// entry `scope` (a dotted name; None: the module), by the tables
    /// alone: (path, dotted name, line, column), or None. `guess`: a name
    /// nothing brings is the one definition of it in the project.
    #[pyo3(signature = (path, chain, scope=None, guess=true))]
    fn resolve(&self, py: Python<'_>, path: String, chain: String, scope: Option<String>, guess: bool) -> Option<(String, String, u32, u32)> {
        let shared = self.shared.clone();
        py.allow_threads(move || shared.answer(&path, None, |ask, file| {
            let parts: Vec<&str> = chain.split('.').collect();
            let &(other, entry, _) = ask.named(file, scope.as_deref(), &parts, guess).last().filter(|hit| hit.2 == parts.len())?;
            let found = &ask.table(other)?.entries[entry as usize];
            Some((ask.path(other).to_string(), found.dotted.to_string(), found.line, found.column))
        }).0)
    }

    /// The uses in the project of the entry `dotted` of file `path`:
    /// [(path, line, column, characters, it is a guess)], or None when there is no such entry.
    fn uses(&self, py: Python<'_>, path: String, dotted: String) -> Option<Vec<Place>> {
        let shared = self.shared.clone();
        py.allow_threads(move || shared.answer(&path, None, |ask, file| {
            let names: Vec<&str> = dotted.split('.').collect();
            let entry = ask.table(file)?.walk(-1, &names).last().filter(|hit| hit.1 == names.len())?.0;
            Some(ask.uses(file, entry))
        }).0)
    }

    /// The entries of file `path` that start on lines `first ..= last`, as
    /// `Source.table` rows: (dotted name, name, kind, line, last line,
    /// indent, column, parent entry, line text, tint).
    #[allow(clippy::type_complexity)]
    fn entries(&self, path: &str, first: u32, last: u32) -> Vec<(String, String, &'static str, u32, u32, u32, u32, i32, String, Vec<f64>)> {
        let index = self.shared.index.read().unwrap();
        let Some(table) = index.ids.get(path).and_then(|&id| index.files[id as usize].table.as_ref()) else { return vec![] };
        let from = table.entries.partition_point(|entry| entry.line < first);
        table.entries[from..].iter().take_while(|entry| entry.line <= last).map(|entry| {
            (entry.dotted.to_string(), entry.name.to_string(), ["class", "def", "var"][entry.kind as usize], entry.line, entry.end, entry.indent, entry.column,
             entry.parent, entry.shown.to_string(), entry.tint.map_or(vec![], |tint| tint.to_vec()))
        }).collect()
    }

    /// The project's entries called `name`: (path, dotted name, kind, line, column, tint).
    #[allow(clippy::type_complexity)]
    fn named(&self, name: &str) -> Vec<(String, String, &'static str, u32, u32, Vec<f64>)> {
        let index = self.shared.index.read().unwrap();
        let key = hash(name);
        let mut files: Vec<u32> = [&index.names, &index.members].iter().filter_map(|names| names.get(&key)).flatten().copied().collect();
        files.sort_unstable();
        files.dedup();
        files.iter().filter_map(|&id| Some((&index.files[id as usize].path, index.files[id as usize].table.as_ref()?))).flat_map(|(path, table)| {
            table.entries.iter().filter(|entry| &*entry.name == name).map(move |entry| {
                (path.clone(), entry.dotted.to_string(), ["class", "def", "var"][entry.kind as usize], entry.line, entry.column, entry.tint.map_or(vec![], |tint| tint.to_vec()))
            })
        }).collect()
    }

    /// Whether anything the answer that gave `needs` hung on changed since.
    fn stale(&self, needs: PyRef<'_, Needs>) -> bool {
        let index = self.shared.index.read().unwrap();
        index.generation != needs.stamp && needs.keys.iter().any(|key| index.moved.get(key).is_some_and(|&moved| moved > needs.stamp))
    }

    /// The generation at which what other files see of `path`'s table last
    /// changed (0: no table), or without a path the roster's own.
    #[pyo3(signature = (path=None))]
    fn generation(&self, path: Option<&str>) -> u64 {
        let index = self.shared.index.read().unwrap();
        match path {
            Some(path) => index.ids.get(path).map_or(0, |&id| index.files[id as usize].generation),
            None => index.generation,
        }
    }

    /// (project files, tables read, entries, uses, bytes held about, files read for imports from outside).
    fn counts(&self) -> (usize, usize, usize, usize, usize, usize) {
        let index = self.shared.index.read().unwrap();
        let tables = || index.files.iter().filter_map(|file| file.table.as_ref());
        let posts: usize = [&index.names, &index.members, &index.posts].iter().map(|posts| posts.values().map(|files| 48 + files.capacity() * 4).sum::<usize>()).sum();
        (index.files.iter().filter(|file| file.inside).count(), tables().count(), tables().map(|table| table.entries.len()).sum(),
         tables().map(|table| table.uses.len()).sum(), tables().map(Table::weight).sum::<usize>() + posts + index.moved.len() * 24,
         index.files.iter().filter(|file| !file.inside && file.table.is_some()).count())
    }

    /// Have the thread do what waits now, and wait for it: every table is in and `wake` was called (tests).
    fn settle(&self, py: Python<'_>) {
        let shared = self.shared.clone();
        py.allow_threads(move || {
            let mut desk = shared.desk.lock().unwrap();
            while desk.running && (desk.waits() || desk.working || desk.news) { desk = shared.bell.wait_timeout(desk, Duration::from_millis(20)).unwrap().0; }
        });
    }

    /// End the thread and wait for it. The roster stays usable: the next push starts one again.
    fn close(&self, py: Python<'_>) {
        self.shared.end();
        let shared = self.shared.clone();
        py.allow_threads(move || shared.join());
        self.shared.desk.lock().unwrap().closed = false;
    }
}

impl Drop for Roster {
    fn drop(&mut self) { self.shared.end(); }
}

/// End every roster's thread: the interpreter is exiting.
#[pyfunction]
fn shut(py: Python<'_>) {
    let live: Vec<Arc<Shared>> = LIVE.lock().unwrap().iter().filter_map(Weak::upgrade).collect();
    py.allow_threads(move || {
        for shared in live {
            shared.end();
            shared.join();
        }
    });
}

/// Give the module the roster's classes, and end its threads at exit.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Roster>()?;
    module.add_class::<Needs>()?;
    module.py().import("atexit")?.call_method1("register", (wrap_pyfunction!(shut, module)?,))?;
    Ok(())
}
