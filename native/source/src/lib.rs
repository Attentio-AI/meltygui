//! `Source`: a text and the facts derived from it, for Python.
//!
//! ```text
//! source = Source(text, version)
//! source.splice(low, high, new)             # every edit, in characters, by anyone
//! source.ask(wake, rest=0.15, rows=rows)    # facts for the text as it is now, for every session on it
//! if source.land():                         # on a later frame: (version, term) of the newest facts
//!     rows.land(source, changes, first, last); source.hits(); source.names(line, indent, prefix)
//! ```
//!
//! The text lives here as shared chunks (`store.rs`) and is the only copy:
//! an editor reads the lines it shows (`blocks`, `texts`, `line` queries by
//! offset) and never the whole of it. `ask` hands the reading thread a
//! snapshot; it reads the facts without the interpreter lock (`facts.rs`,
//! on per chunk tokens and bindings cached in `scan.rs` and `bind.rs`), then
//! wakes whoever waits through C handles of the frame loop (`Waker`): the
//! thread never runs Python, so it can do no harm to an interpreter that is
//! exiting. The newest request wins: a snapshot the thread has not
//! started on is replaced, and with `rest` it waits that long for a newer
//! one first, so a burst of edits is read once.
//!
//! Landing costs nothing per fact: the facts stay here and cross to Python
//! where they are asked for, by line. Several sessions may be on one source:
//! one reading holds the fold ranges as each of them folds them and the hits
//! of each one's search term. A session's `Rows` (`rows.rs`) takes its fold
//! ranges and keeps them on their lines, `Hits` its search hits;
//! `defs` and `read` convert whole tables for callers that want plain values.
pub mod bind;
pub mod call;
pub mod colour;
pub mod facts;
pub mod roster;
pub mod rows;
pub mod scan;
pub mod store;

use std::collections::HashSet;
use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::time::{Duration, Instant};

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyCapsule, PyList, PyString, PyTuple};

use facts::{Facts, Name, Specs};
use rows::{Hits, Parcel, Rows, Shut};
use store::{Chunk, Store};

/// Lever: seconds without a request before the reading thread ends.
const IDLE: u64 = 20;

/// Every source's thread state, to end the threads when the interpreter exits.
static LIVE: Mutex<Vec<Weak<Shared>>> = Mutex::new(Vec::new());
static EXITING: AtomicBool = AtomicBool::new(false);

struct Job {
    chunks: Vec<Arc<Chunk>>,
    version: u64,
    blocks: bool,
    /// The search terms of the sessions on the source, each once.
    terms: Vec<String>,
    /// Each session's collapsed fold names, and whether its default folds are still to be applied.
    folds: Vec<(Arc<Shut>, bool)>,
    /// Look for undefined names and misfitting calls, given the names the
    /// live module has and the signatures of what its calls reach.
    lint: Option<(Arc<HashSet<String>>, Arc<Specs>, u64)>,
    /// Source site keys to find the lines of (breakpoints); the statement table is made with them.
    sites: Option<Vec<Vec<String>>>,
    /// Who is told when the reading waits: without the interpreter.
    wake: Vec<Waker>,
    /// When the thread may start on it.
    due: Instant,
}

/// One reading: the facts, and what each session on the source takes from them.
pub(crate) struct Found {
    pub(crate) facts: Arc<Facts>,
    /// The fold ranges as each session folds them, until that session took its own.
    pub(crate) folds: Vec<Parcel>,
    /// The occurrences of each search term.
    hits: Vec<(String, Hits)>,
    /// The undefined names (`Facts::undefined`), when they were asked for.
    undefined: Vec<(u32, u32, String, bool, bool)>,
    /// The calls that do not fit what they call (`Facts::miscalls`), likewise.
    miscalls: Vec<(u32, u32, String)>,
    /// The chains called whose signatures were not there (`Source::callees`).
    callees: Vec<String>,
    /// The chunks the lint read: the others kept their findings (`Facts::lint`).
    relinted: u32,
    /// The site keys asked for with the line each one starts on (`Source::placed`).
    placed: Vec<(Vec<String>, Option<u32>)>,
}

#[derive(Default)]
struct Desk {
    job: Option<Job>,
    /// The newest reading nobody took yet.
    found: Option<Found>,
    working: bool,
    running: bool,
    closed: bool,
    /// The names the running module of this text has (`Source::live`).
    live: Arc<HashSet<String>>,
    /// The signatures of what its calls reach (`Source::signatures`), and how often they were told.
    specs: Arc<Specs>,
    serial: u64,
    /// The project roster the source's readings go to, and the file it is there (`Source::join`).
    roster: Option<(Arc<roster::Shared>, String)>,
}

#[derive(Default)]
struct Shared {
    desk: Mutex<Desk>,
    bell: Condvar,
}

impl Shared {
    /// Tell the thread to end; a request it has not started on is dropped.
    fn end(&self) {
        let mut desk = self.desk.lock().unwrap();
        desk.closed = true;
        desk.job = None;
        self.bell.notify_all();
    }

    /// Wait for the thread to end.
    fn join(&self) {
        let mut desk = self.desk.lock().unwrap();
        while desk.running { desk = self.bell.wait(desk).unwrap(); }
    }
}

/// A wake by C ABI, as the GUI crate hands it out (`_gui_native.Wakes`,
/// native/gui_prototype/src/wake.rs): `wake(data, token)` queues the token
/// for the frame loop and rouses its event wait. Called on the reading
/// thread, which never takes the interpreter lock.
#[repr(C)]
struct Handle {
    wake: unsafe extern "C" fn(*const c_void, u64),
    keep: unsafe extern "C" fn(*const c_void),
    free: unsafe extern "C" fn(*const c_void),
    data: *const c_void,
}

struct Waker {
    wake: unsafe extern "C" fn(*const c_void, u64),
    free: unsafe extern "C" fn(*const c_void),
    data: *const c_void,
    token: u64,
}

unsafe impl Send for Waker {}

impl Waker {
    fn new(handle: &Bound<'_, PyCapsule>, token: u64) -> PyResult<Waker> {
        if handle.name()?.map(|name| name.to_bytes()) != Some(b"meltygui.wake") {
            return Err(pyo3::exceptions::PyTypeError::new_err("not a wake handle (`_gui_native.Wakes.handle()`)"));
        }
        let handle = unsafe { &*(handle.pointer() as *const Handle) };
        unsafe { (handle.keep)(handle.data) };
        Ok(Waker { wake: handle.wake, free: handle.free, data: handle.data, token })
    }
}

impl Drop for Waker {
    fn drop(&mut self) { unsafe { (self.free)(self.data) } }
}

fn read(job: Job) -> (Found, Vec<Waker>) {
    let hits = job.terms.iter().filter(|term| !term.is_empty()).map(|term| (term.clone(), Hits::new(facts::find(&job.chunks, term)))).collect();
    let mut facts = Facts::read(job.chunks, job.version, job.blocks);
    facts.term = job.terms.into_iter().next().unwrap_or_default();
    let folds = job.folds.into_iter().map(|(names, seeded)| Parcel::new(&facts, names, !seeded)).collect();
    let empty = HashSet::new();
    let (undefined, miscalls, callees, relinted) = job.lint.map_or((vec![], vec![], vec![], 0), |(live, specs, serial)| {
        facts.lint(facts::KNOWN.get().unwrap_or(&empty), &live, &specs, serial)
    });
    let placed = job.sites.map_or(vec![], |keys| {
        let lines = facts.places(&keys);
        keys.into_iter().zip(lines).collect()
    });
    (Found { facts: Arc::new(facts), folds, hits, undefined, miscalls, callees, relinted, placed }, job.wake)
}

/// A thread that died reading must not leave `settle` and `close` waiting.
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

fn work(shared: Arc<Shared>) {
    let _shift = Shift(&shared);
    loop {
        let job = {
            let mut desk = shared.desk.lock().unwrap();
            loop {
                if desk.closed { desk.running = false; shared.bell.notify_all(); return; }
                let now = Instant::now();
                match desk.job.as_ref().map(|job| job.due) {
                    Some(due) if due <= now => {
                        desk.working = true;
                        break desk.job.take().unwrap();
                    }
                    Some(due) => desk = shared.bell.wait_timeout(desk, due - now).unwrap().0,
                    None => {
                        let (next, wait) = shared.bell.wait_timeout(desk, Duration::from_secs(IDLE)).unwrap();
                        desk = next;
                        if wait.timed_out() && desk.job.is_none() { desk.running = false; shared.bell.notify_all(); return; }
                    }
                }
            }
        };
        let (found, wake) = read(job);
        shared.desk.lock().unwrap().found = Some(found);
        // Nothing here touches the interpreter: a wake is a token queued for the frame loop.
        for waker in &wake { unsafe { (waker.wake)(waker.data, waker.token) } }
        drop(wake);
        // Only now is the request done: `settle` returns after the wake.
        shared.desk.lock().unwrap().working = false;
        shared.bell.notify_all();
    }
}

/// A Python string as UTF-8. Encoded here instead of borrowed, so the
/// string keeps no second copy of itself; a lone surrogate (not text) reads
/// as '?', one character, so offsets hold.
fn utf8<'py>(text: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyBytes>> {
    Ok(text.call_method1("encode", ("utf-8", "replace"))?.downcast_into()?)
}

/// The bytes `utf8` returned as text: Python's own encoding, not checked again.
fn decoded<'a>(bytes: &'a Bound<'_, PyBytes>) -> &'a str {
    unsafe { std::str::from_utf8_unchecked(bytes.as_bytes()) }
}

pub(crate) fn name<'py>(py: Python<'py>, name: &Name) -> PyResult<Bound<'py, PyTuple>> {
    let mut parts: Vec<Bound<'py, PyAny>> = Vec::with_capacity(name.parts.len() + 1);
    for part in &name.parts { parts.push(part.into_pyobject(py)?.into_any()); }
    if name.twin > 0 { parts.push(name.twin.into_pyobject(py)?.into_any()); }
    PyTuple::new(py, parts)
}

/// (is class, name, line, last line, indent, parent index) of every definition.
fn defs(facts: &Facts) -> Vec<(bool, &str, u32, u32, u32, i32)> {
    facts.defs.iter().enumerate()
        .map(|(at, def)| (def.class, facts.def(at), def.line, def.last, def.indent, def.parent)).collect()
}

#[pyclass(module = "meltygui.model._source_native")]
pub struct Source {
    store: Store,
    version: u64,
    shared: Arc<Shared>,
    /// The newest reading that arrived.
    pub(crate) landed: Option<Found>,
}

impl Source {
    #[allow(clippy::too_many_arguments)]
    fn job(&self, mut terms: Vec<String>, blocks: bool, rows: Vec<PyRef<'_, Rows>>, wake: Vec<Waker>, rest: f64, lint: bool,
           sites: Option<Vec<Vec<String>>>) -> Job {
        let mut seen = HashSet::new();
        terms.retain(|term| seen.insert(term.clone()));
        Job { chunks: self.store.chunks.clone(), version: self.version, blocks,
              terms, folds: rows.iter().map(|rows| (rows.shut.clone(), rows.seeded)).collect(), wake,
              lint: lint.then(|| {
                  let desk = self.shared.desk.lock().unwrap();
                  (desk.live.clone(), desk.specs.clone(), desk.serial)
              }),
              sites,
              due: Instant::now() + Duration::from_secs_f64(rest.max(0.0)) }
    }
}

/// Let go of a reading's facts. Whoever holds them last frees every table
/// of the text, which costs its size (5 ms at 100k lines): the callers are
/// on the render thread, so the last holder frees them on a thread of its own.
pub(crate) fn retire(facts: Arc<Facts>) {
    if Arc::strong_count(&facts) == 1 {
        let _ = std::thread::Builder::new().name("melty-source-retire".into()).spawn(move || drop(facts));
    }
}

#[pymethods]
impl Source {
    #[new]
    #[pyo3(signature = (text=None, version=0))]
    fn new(text: Option<&Bound<'_, PyString>>, version: u64) -> PyResult<Source> {
        let bytes = text.map(utf8).transpose()?;
        let store = Store::new(bytes.as_ref().map_or("", decoded));
        let shared = Arc::<Shared>::default();
        let mut live = LIVE.lock().unwrap();
        live.retain(|other| other.strong_count() > 0);
        live.push(Arc::downgrade(&shared));
        Ok(Source { store, version, shared, landed: None })
    }

    /// Put `new` in place of characters `low .. high`; the text is then at
    /// `version` (by default the next one). Returns what the edit did in
    /// lines and columns: the text from (line, column) over `gone` line
    /// breaks to column `old` became text over `came` line breaks ending at
    /// column `new`.
    #[pyo3(signature = (low, high, new, version=None))]
    fn splice(&mut self, low: usize, high: usize, new: &Bound<'_, PyString>, version: Option<u64>) -> PyResult<(usize, usize, usize, usize, usize, usize)> {
        let version = version.unwrap_or(self.version + 1);
        let bytes = utf8(new)?;
        let new = decoded(&bytes);
        let ((line, column), (last, old)) = (self.store.place(low), self.store.place(high.max(low)));
        let came = new.bytes().filter(|&byte| byte == b'\n').count();
        let tail = new.rfind('\n').map_or(column + new.chars().count(), |at| new[at + 1..].chars().count());
        self.store.splice(low, high, new, version);
        self.version = version;
        Ok((line, column, last - line, old, came, tail))
    }

    #[getter]
    fn version(&self) -> u64 { self.version }

    /// Characters in the text.
    #[getter]
    fn size(&self) -> usize { self.store.chars }

    #[getter]
    fn lines(&self) -> usize { self.store.breaks + 1 }

    fn text(&self) -> String { self.store.text() }

    /// The blocks holding lines `first ..= last`: (key, first line, lines,
    /// stamp). A block is a run of lines the editor paints as one view; its
    /// key stays with it through edits and its stamp is the version of the
    /// edit that last rewrote it.
    fn blocks(&mut self, first: usize, last: usize) -> Vec<(u64, usize, usize, u64)> {
        let (from, _) = self.store.line(first);
        let most = self.store.chunks.len() - 1;
        let (from, to) = (from.min(most), self.store.line(last.max(first)).0.min(most));
        let mut line = self.store.lines_before(from);
        (from..=to).map(|at| {
            // The last block also holds the text's last line, which has no break.
            let count = self.store.chunks[at].breaks as usize + (at + 1 == self.store.chunks.len()) as usize;
            line += count;
            (self.store.keys[at], line - count, count, self.store.stamps[at])
        }).collect()
    }

    /// The text of `count` lines from `first`, without their breaks.
    fn texts(&mut self, first: usize, count: usize) -> Vec<String> {
        let mut found = Vec::with_capacity(count);
        let last = (first + count).min(self.store.breaks + 1);
        let mut line = first;
        while line < last {
            let (at, local) = self.store.line(line);
            let chunk = &self.store.chunks[at];
            let rows = &chunk.scan().lines;
            if local >= rows.len() {
                found.push(String::new());
                line += 1;
            }
            for row in rows.iter().skip(local).take(last - line) {
                found.push(chunk.text[row.start as usize..row.end as usize].to_string());
                line += 1;
            }
        }
        found
    }

    /// The character offset line `line` starts at.
    fn start(&mut self, line: usize) -> usize { self.store.start(line) }

    /// (line, column) of character offset `index`.
    fn place(&mut self, index: usize) -> (usize, usize) { self.store.place(index) }

    /// The text between two character offsets.
    fn slice(&mut self, low: usize, high: usize) -> String { self.store.slice(low, high) }

    /// (text, where it starts): the text around offset `index`, from at
    /// least `reach` characters before it to the end of the block after its own.
    fn near(&mut self, index: usize, reach: usize) -> (String, usize) { self.store.near(index, reach) }

    /// The string state each of `count` lines from `first` starts in, for
    /// `runs`: 0 in code, 255 not known yet (the text above was never lexed:
    /// it is once a reading has landed). 16 is added for a line that starts
    /// right inside a call's parentheses (`Strings::calls`).
    fn states(&mut self, first: usize, count: usize) -> Vec<u8> {
        let mut found = Vec::with_capacity(count);
        let last = self.store.breaks;
        while found.len() < count && first + found.len() <= last {
            let (at, local) = self.store.line(first + found.len());
            let Some(entry) = self.store.entry(at) else {
                found.resize(count, 255);
                break;
            };
            let strings = self.store.chunks[at].strings(entry);
            for line in local..strings.lines.len() {
                if found.len() == count { break; }
                found.push(if line == 0 { entry } else { strings.lines[line - 1] >> 2 } | (strings.calls[line] as u8) << 4);
            }
            // (The text's last line, after its last break, is in no chunk when it is empty.)
            if local >= strings.lines.len() { found.push(strings.exit); }
        }
        found.resize(count, 0);
        found
    }

    /// Whether character `column` of line `line` is inside a string or a comment.
    fn inside(&mut self, line: usize, column: usize) -> bool {
        let (at, local) = self.store.line(line);
        let chunk = self.store.chunks[at].clone();
        let Some(strings) = self.store.entry(at).map(|entry| chunk.strings(entry)) else { return false };
        if local >= strings.lines.len() { return strings.exit != 0; }
        let row = &chunk.scan().lines[local];
        let text = &chunk.text[row.start as usize..row.end as usize];
        let byte = row.start as usize + text.char_indices().nth(column).map_or(text.len(), |(byte, _)| byte);
        let entered = strings.lines[local] & 1 != 0;
        strings.on(local).iter().any(|tok| {
            let (start, end) = (tok.start as usize, tok.start as usize + tok.len as usize);
            // A comment holds its end, and so does a string that does not close on the line.
            let open = tok.kind == scan::NOTE || (end == row.end as usize && (strings.lines[local] >> 2 != 0
                || end - start < 2 || !matches!(chunk.text.as_bytes()[end - 1], b'"' | b'\'')));
            matches!(tok.kind, scan::STRING | scan::NOTE) && (start < byte || (entered && start == row.start as usize))
                && (byte < end || (open && byte == end))
        })
    }

    /// Ask for the facts of the text as it is now: `land` answers once the
    /// thread has read them, and that thread then wakes every `(handle,
    /// token)` of `wake` (`_gui_native.Wakes.handle()`): by C ABI, never
    /// through the interpreter.
    /// `term`: also find its occurrences. `blocks`: compound statements fold.
    /// `rest`: seconds to wait for a newer request before reading. `rows`:
    /// a session whose `Rows.land` takes the fold ranges. `now`: read on
    /// the calling thread instead (small texts); `land` answers at once.
    /// `lint`: also look for undefined names (`undefined`). `sites`: source
    /// site key paths to find the lines of (`placed`); with them the
    /// statement table is made on the reading thread and `site` answers.
    /// `sessions`: the (term, rows) of every further session on the source:
    /// one reading serves them all.
    #[pyo3(signature = (wake=None, term=None, blocks=true, rest=0.0, rows=None, now=false, lint=false, sites=None, sessions=vec![]))]
    #[allow(clippy::too_many_arguments)]
    fn ask(&self, py: Python<'_>, wake: Option<Vec<(Bound<'_, PyCapsule>, u64)>>, term: Option<String>, blocks: bool, rest: f64,
           rows: Option<PyRef<'_, Rows>>, now: bool, lint: bool, sites: Option<Vec<Vec<String>>>,
           sessions: Vec<(String, PyRef<'_, Rows>)>) -> PyResult<()> {
        if EXITING.load(Ordering::Relaxed) { return Ok(()); }
        let (mut terms, mut all): (Vec<String>, Vec<PyRef<'_, Rows>>) = (term.into_iter().collect(), rows.into_iter().collect());
        for (term, rows) in sessions {
            terms.push(term);
            all.push(rows);
        }
        let wake = if now { vec![] } else { wake.unwrap_or_default().iter().map(|(handle, token)| Waker::new(handle, *token)).collect::<PyResult<_>>()? };
        let job = self.job(terms, blocks, all, wake, rest, lint, sites);
        if now {
            let found = py.allow_threads(|| read(job).0);
            let mut desk = self.shared.desk.lock().unwrap();
            (desk.job, desk.found) = (None, Some(found));
            return Ok(());
        }
        let mut desk = self.shared.desk.lock().unwrap();
        desk.job = Some(job);
        desk.closed = false;
        if !desk.running {
            desk.running = true;
            let shared = self.shared.clone();
            std::thread::Builder::new().name("melty-source".into()).spawn(move || work(shared)).unwrap();
        }
        self.shared.bell.notify_all();
        Ok(())
    }

    /// Take the newest reading the thread finished: (version, term) of its
    /// facts, once; None when nothing new arrived.
    fn land(&mut self) -> Option<(u64, String)> {
        let found = self.shared.desk.lock().unwrap().found.take()?;
        if self.landed.as_ref().is_some_and(|landed| landed.facts.version > found.facts.version) { return None; }
        if let Some(old) = self.landed.replace(found) { retire(old.facts); }
        let facts = self.landed.as_ref().unwrap().facts.clone();
        // The reading is its file's table in the project roster from now on (made on the roster's thread).
        let roster = self.shared.desk.lock().unwrap().roster.clone();
        if let Some((roster, path)) = roster { roster.hand(&path, &facts); }
        Some((facts.version, facts.term.clone()))
    }

    /// Give every reading landed from now on to the project `roster` as
    /// the table of file `path` (`roster.rs`); without a roster: no more.
    #[pyo3(signature = (roster=None, path=None))]
    fn join(&self, roster: Option<PyRef<'_, roster::Roster>>, path: Option<String>) {
        let roster = roster.zip(path).map(|(roster, path)| (roster.shared.clone(), path));
        if let (Some((roster, path)), Some(found)) = (&roster, &self.landed) { roster.hand(path, &found.facts); }
        self.shared.desk.lock().unwrap().roster = roster;
    }

    /// The names the running module of this text has, as plain data: a
    /// name among them is not reported undefined. Pushed from Python
    /// whenever they may have changed (an import, a hotswap), from any
    /// thread; used by the readings asked for after.
    fn live(&self, names: Vec<String>) {
        self.shared.desk.lock().unwrap().live = Arc::new(names.into_iter().collect());
    }

    /// The undefined names of the landed reading (asked with `lint`):
    /// (line, column, name, the live module has it, it is in an annotation).
    fn undefined(&self) -> Vec<(u32, u32, &str, bool, bool)> {
        self.landed.as_ref().map_or(vec![], |found| {
            found.undefined.iter().map(|(line, column, name, live, note)| (*line, *column, name.as_str(), *live, *note)).collect()
        })
    }

    /// The calls of the landed reading (asked with `lint`) that do not fit
    /// the definition in this text they call: (line, column, message).
    fn miscalls(&self) -> Vec<(u32, u32, &str)> {
        self.landed.as_ref().map_or(vec![], |found| found.miscalls.iter().map(|(line, column, message)| (*line, *column, message.as_str())).collect())
    }

    /// Drop the lint findings kept on the chunks: the next reading reads
    /// them all again (what the kept ones are tested against).
    fn forget(&self) {
        for chunk in &self.store.chunks { *chunk.lint.lock().unwrap() = None; }
    }

    /// What cannot be Python in the landed reading's text, as far as the
    /// reader tells for sure: (line, CPython's message). See `Facts::flaws`.
    fn flaws(&self) -> Vec<(u32, &str)> {
        self.landed.as_ref().map_or(vec![], |found| found.facts.flaws.iter().map(|(line, message)| (*line, message.as_str())).collect())
    }

    /// The landed reading's top-level statements as (first line, last
    /// line, key): each compiles on its own, and the key is the same while
    /// its text is. See `Facts::tops`.
    fn tops(slf: PyRef<'_, Self>, py: Python<'_>) -> Vec<(u32, u32, u64)> {
        let facts = slf.landed.as_ref().map(|found| found.facts.clone());
        // (Not borrowed while the interpreter is let go: the writer may edit meanwhile.)
        drop(slf);
        py.allow_threads(|| facts.map_or(vec![], |facts| facts.tops()))
    }

    /// The landed reading's text of lines `first ..= last`.
    fn span(&self, first: u32, last: u32) -> String {
        self.landed.as_ref().map_or(String::new(), |found| found.facts.span(first, last))
    }

    /// The landed reading's symbol table as plain rows: (entries, imports,
    /// star imports). See `Facts::table`.
    #[allow(clippy::type_complexity)]
    fn table(slf: PyRef<'_, Self>, py: Python<'_>) -> (Vec<(String, String, &'static str, u32, u32, u32, u32, i32, String, Vec<f64>)>, Vec<(String, String, Option<String>)>, Vec<String>) {
        let facts = slf.landed.as_ref().map(|found| found.facts.clone());
        // (Not borrowed while the interpreter is let go: the writer may edit meanwhile.)
        drop(slf);
        py.allow_threads(|| facts.map_or((vec![], vec![], vec![]), |facts| facts.table()))
    }

    /// What the definition tints of the landed reading paint: (line,
    /// column, characters, red, green, blue), a row of 0 characters for a
    /// tinted definition's first line. See `Facts::tints`.
    fn tints(slf: PyRef<'_, Self>, py: Python<'_>) -> Vec<(u32, u32, u32, f64, f64, f64)> {
        let facts = slf.landed.as_ref().map(|found| found.facts.clone());
        // (Not borrowed while the interpreter is let go: the writer may edit meanwhile.)
        drop(slf);
        py.allow_threads(|| facts.map_or(vec![], |facts| facts.tints()))
    }

    /// What the name at `column` of `line` leads to in the landed reading:
    /// (it is a definition, [(line, column, characters)]): the definition
    /// of a symbol used there, or the uses of the one defined there.
    fn lookup(slf: PyRef<'_, Self>, py: Python<'_>, line: u32, column: u32) -> Option<(bool, Vec<(u32, u32, u32)>)> {
        let facts = slf.landed.as_ref().map(|found| found.facts.clone());
        // (Not borrowed while the interpreter is let go: the writer may edit meanwhile.)
        drop(slf);
        py.allow_threads(|| facts.and_then(|facts| facts.lookup(line, column)))
    }

    /// Where the code of lines `first ..= last` stands: (line, start column,
    /// end column, right-hand side or None). See `Facts::spans`.
    #[allow(clippy::type_complexity)]
    fn spans(&self, first: u32, last: u32) -> Vec<(u32, u32, u32, Option<(u32, u32)>)> {
        self.landed.as_ref().map_or(vec![], |found| found.facts.spans(first, last))
    }

    /// Where the name `label` stands on `line`: (column, end column) or None. See `Facts::label`.
    fn label(&self, line: u32, label: &str) -> Option<(u32, u32)> {
        self.landed.as_ref().and_then(|found| found.facts.label(line, label))
    }

    /// Where the calls starting on lines `first ..= last` close: (line,
    /// column, last line, end column). See `Facts::closes`.
    fn closes(&self, first: u32, last: u32) -> Vec<(u32, u32, u32, u32)> {
        self.landed.as_ref().map_or(vec![], |found| found.facts.closes(first, last))
    }

    /// The landed reading's statement table: (line, column, last line, end
    /// column, kind, key path) of every statement and compound header: the
    /// source sites breakpoints are kept by. See `Facts::sites`.
    #[allow(clippy::type_complexity)]
    fn sites(slf: PyRef<'_, Self>, py: Python<'_>) -> Vec<(u32, u32, u32, u32, &'static str, Vec<String>)> {
        let facts = slf.landed.as_ref().map(|found| found.facts.clone());
        // (Not borrowed while the interpreter is let go: the writer may edit meanwhile.)
        drop(slf);
        py.allow_threads(|| facts.map_or(vec![], |facts| facts.sites()))
    }

    /// (first line, key path) of the landed reading's source sites that
    /// cover `line`, in source order: what a breakpoint on that line is kept
    /// by. Empty unless the reading was asked for with `sites`.
    fn site(&self, line: u32) -> Vec<(u32, Vec<String>)> {
        self.landed.as_ref().map_or(vec![], |found| found.facts.site(line))
    }

    /// (key path, first line or None) of the site keys the landed reading was asked for.
    fn placed(&self) -> Vec<(Vec<String>, Option<u32>)> {
        self.landed.as_ref().map_or(vec![], |found| found.placed.clone())
    }

    /// How many chunks the landed reading's lint read: the others kept
    /// what was found in them before (`Facts::lint`).
    fn relinted(&self) -> u32 {
        self.landed.as_ref().map_or(0, |found| found.relinted)
    }

    /// The chains the landed reading's calls reach in the running process
    /// (from a name only imports bind; `builtins.name` for a name nothing
    /// binds) whose signatures `signatures` has not given yet.
    fn callees(&self) -> Vec<&str> {
        self.landed.as_ref().map_or(vec![], |found| found.callees.iter().map(String::as_str).collect())
    }

    /// The signatures of what the calls reach, as plain data: {chain: rows
    /// of (name, 0 positional only / 1 positional or keyword / 2 keyword
    /// only / 3 `*` / 4 `**`, it has no default, annotated type or ""), or
    /// None when it is not to be judged}. Pushed from Python like `live`.
    fn signatures(&self, specs: Specs) {
        let mut desk = self.shared.desk.lock().unwrap();
        (desk.specs, desk.serial) = (Arc::new(specs), desk.serial + 1);
    }

    /// The landed occurrences of `term` (by default the first one asked
    /// for), or None when the reading did not look for it. A copy of its
    /// own for each caller: it follows that caller's edits from then on.
    #[pyo3(signature = (term=None))]
    fn hits(&self, term: Option<&str>) -> Option<Hits> {
        let found = self.landed.as_ref()?;
        let term = term.unwrap_or(&found.facts.term);
        found.hits.iter().find(|hits| hits.0 == term).map(|hits| hits.1.clone())
    }

    /// The landed definitions: (is class, name, line, last line, indent, parent index).
    fn defs(&self) -> Vec<(bool, &str, u32, u32, u32, i32)> {
        self.landed.as_ref().map_or(vec![], |found| defs(&found.facts))
    }

    /// Completion candidates at `line` (indented `indent` characters) among
    /// the landed facts, as (name, kind): the names bound in the scopes
    /// around the line, innermost first, then other names written near it.
    /// Only names holding `prefix`, case blind.
    fn names(&self, line: u32, indent: u32, prefix: &str) -> Vec<(&str, &'static str)> {
        self.landed.as_ref().map_or(vec![], |found| found.facts.names(line, indent, prefix))
    }

    /// (kind, line) of the binding `name` resolves to from `line`, or None.
    fn bound(&self, name: &str, line: u32, indent: u32) -> Option<(&'static str, u32)> {
        self.landed.as_ref()?.facts.bound(name, line, indent)
    }

    /// The landed bindings on lines `first ..= last`: (line, column, name,
    /// kind, index of the definition whose scope it is bound in or -1).
    fn bindings(&self, first: u32, last: u32) -> Vec<(u32, u32, &str, &'static str, i32)> {
        let Some(found) = &self.landed else { return vec![] };
        let facts = &found.facts;
        let from = facts.binds.partition_point(|bound| bound.line < first);
        facts.binds[from..].iter().take_while(|bound| bound.line <= last)
            .map(|bound| (bound.line, facts.column(bound), facts.word(bound), bind::KINDS[bound.kind as usize], bound.scope)).collect()
    }

    /// The landed references on lines `first ..= last`: (line, column,
    /// dotted chain, the statement binds the name). Every name the text
    /// uses, but not what a `def`, `class`, parameter, import, `except`,
    /// `global` or keyword argument only names.
    fn references(&self, first: u32, last: u32) -> Vec<(u32, u32, &str, bool)> {
        self.landed.as_ref().map_or(vec![], |found| found.facts.references(first, last))
    }

    /// The landed calls of dotted chains that start on lines `first ..= last`:
    /// (line, column, chain, positional arguments or -1 when the call's end
    /// is out of reach, keyword names, has a `*` argument, has a `**` argument).
    fn calls(&self, first: u32, last: u32) -> Vec<(u32, u32, &str, i32, Vec<&str>, bool, bool)> {
        self.landed.as_ref().map_or(vec![], |found| found.facts.calls(first, last))
    }

    /// The landed statements that start on lines `first ..= last`: (first
    /// line, last line), a compound statement being its header.
    fn statements(&self, first: u32, last: u32) -> Vec<(u32, u32)> {
        self.landed.as_ref().map_or(vec![], |found| found.facts.statements(first, last))
    }

    /// The parameters of the function whose `def` is on `line`, by the
    /// landed facts: (name, line, column, where the default value is as
    /// (line, column, last line, end column) or None).
    #[allow(clippy::type_complexity)]
    fn parameters(&self, line: u32) -> Vec<(&str, u32, u32, Option<(u32, u32, u32, u32)>)> {
        self.landed.as_ref().map_or(vec![], |found| found.facts.parameters(line))
    }

    /// The facts of the text as it is now as plain values, read on the
    /// calling thread: (version, folds, defs, hits, bent). `folds` is (first
    /// lines, spans, parent indexes, names, (indent, is def or class) of the
    /// scopes and blocks, names that start collapsed), outermost first;
    /// `hits` (line, column, end column) of `term`; `bent` counts the lines
    /// where the legacy fold scanner would have read a continued statement
    /// as a new one.
    #[pyo3(signature = (term=None, blocks=true))]
    fn read<'py>(&self, py: Python<'py>, term: Option<String>, blocks: bool) -> PyResult<Bound<'py, PyAny>> {
        let job = self.job(term.into_iter().collect(), blocks, vec![], vec![], 0.0, false, None);
        let found = py.allow_threads(|| read(job).0);
        let facts = &found.facts;
        let (names, defaults) = (PyList::empty(py), PyList::empty(py));
        for at in 0..facts.heads.len() { names.append(name(py, &facts.name(at))?)?; }
        for &at in &facts.defaults { defaults.append(names.get_item(at as usize)?)?; }
        for meta in facts.metas() { defaults.append(name(py, &meta)?)?; }
        let scopes: Vec<_> = (0..facts.heads.len()).map(|at| facts.scope(at)).collect();
        let folds = (&facts.heads, &facts.spans, &facts.parents, names, scopes, defaults);
        let hits = found.hits.first().map_or(vec![], |hits| hits.1.on(0, u32::MAX));
        Ok((facts.version, folds, defs(facts), hits, facts.bent).into_pyobject(py)?.into_any())
    }

    /// End the reading thread and wait for it. The source stays usable: the
    /// next `ask` starts a thread again.
    fn close(&self, py: Python<'_>) {
        self.shared.end();
        let shared = self.shared.clone();
        py.allow_threads(move || shared.join());
    }

    /// Have the thread read what was asked for now, and wait for it (tests, exit).
    fn settle(&self, py: Python<'_>) {
        let shared = self.shared.clone();
        py.allow_threads(move || {
            let mut desk = shared.desk.lock().unwrap();
            if let Some(job) = desk.job.as_mut() { job.due = Instant::now(); }
            shared.bell.notify_all();
            while desk.job.is_some() || desk.working { desk = shared.bell.wait(desk).unwrap(); }
        });
    }
}

impl Drop for Source {
    fn drop(&mut self) { self.shared.end(); }
}

/// End every reading thread and start no more: the interpreter is exiting.
#[pyfunction]
fn close(py: Python<'_>) {
    EXITING.store(true, Ordering::Relaxed);
    let live: Vec<Arc<Shared>> = LIVE.lock().unwrap().iter().filter_map(Weak::upgrade).collect();
    py.allow_threads(move || {
        for shared in live {
            shared.end();
            shared.join();
        }
    });
}

/// The colour runs of `line`, which starts in string state `state`
/// (`Source.states`, with its 16 for a line that starts inside a call):
/// (column, characters, kind), neighbours of one plain kind joined. The
/// kinds are the editor's colour keys (`colour.rs`).
#[pyfunction]
fn runs(line: &str, state: u8) -> Vec<(u32, u32, &'static str)> {
    let known = state != 255;
    colour::runs(line, if known && state & 15 <= 6 { state & 15 } else { 0 }, known && state & 16 != 0)
}

/// The names every module sees without binding them (once: the running
/// interpreter's builtins and a module's own dunders).
#[pyfunction]
fn known(names: Vec<String>) {
    let _ = facts::KNOWN.set(names.into_iter().collect());
}

/// The names coloured as builtins (once: the running interpreter's).
#[pyfunction]
fn builtins(names: Vec<String>) {
    let _ = colour::BUILTINS.set(names.into_iter().collect());
}

#[pymodule]
fn _source_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("LINES", store::LINES)?;
    module.add_function(wrap_pyfunction!(runs, module)?)?;
    module.add_function(wrap_pyfunction!(builtins, module)?)?;
    module.add_function(wrap_pyfunction!(known, module)?)?;
    module.add_class::<Source>()?;
    module.add_class::<Rows>()?;
    module.add_class::<Hits>()?;
    roster::register(module)?;
    module.py().import("atexit")?.call_method1("register", (wrap_pyfunction!(close, module)?,))?;
    Ok(())
}
