//! A text kept in step with its file: the io under `Source(Toggles)`
//! (`meltygui/model/source_file_model.py`).
//!
//! ```text
//! native.open(path)     # nothing is read here: the io thread loads the file and reads its facts
//! native.arrive()       # render thread: take what came from the file (its text, a change another program made)
//! native.splice(..)     # every edit follows to the file by itself
//! ```
//!
//! One thread (`melty-file`) does the io of every file and never holds the
//! interpreter lock. It computes nothing: a loaded text's facts are read on a
//! second thread (`melty-file-read`), so a long file holds up no other file's io. It keeps its own copy of each text's chunk list, which
//! an edit follows by the chunks it replaced (`Task::Edit`: an edit costs
//! its chunk here too, whatever the file size), so it writes and compares
//! without asking the render thread for anything.
//!
//! - **Load.** The file is read and cut into chunks here, and its facts are
//!   read with the tree (`reader`); the render thread installs the chunk
//!   list (`Arrival::Loaded`) and lands the reading.
//! - **Write.** The whole text to a temp name beside the file, renamed over
//!   it; no fsync. Edits that pile up while one is written are one write:
//!   the newest text wins. There is no timer.
//! - **Follow.** A change by another program arrives as an inotify event
//!   (`watch`, Linux), is read here and compared with the copy; what differs
//!   crosses as one splice (`Arrival::Changed`) the render thread applies.
//!   A write of our own is known by the file's `Stamp` taken as it was
//!   written, never by its text.
//! - **Never over a state nobody read.** Before a write the file's stamp
//!   must be the one last read or written; when it is not, the file is read
//!   instead and the text takes it: the file wins over an edit not yet written.
//!
//! Levers: `write` (the temp name, fsync), `Stamp` (what tells two states
//! of a file apart), `pull` (encodings: UTF-8 with or without a BOM; any
//! other file is shown and never written), `watch::MASK` (which events
//! count). Another platform's watcher is a `watch` module with `add`.
use std::fs;
use std::io::{BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Instant, SystemTime};

use crate::store::{self, Chunk};
use crate::{Job, Shared, Waker};

const BOM: &[u8] = b"\xEF\xBB\xBF";

/// What tells one state of a file from another without reading it.
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct Stamp {
    modified: Option<SystemTime>,
    size: u64,
    inode: u64,
}

impl Stamp {
    fn of(meta: &fs::Metadata) -> Stamp {
        #[cfg(unix)]
        let inode = std::os::unix::fs::MetadataExt::ino(meta);
        #[cfg(not(unix))]
        let inode = 0;
        Stamp { modified: meta.modified().ok(), size: meta.len(), inode }
    }

    fn at(path: &Path) -> Option<Stamp> {
        fs::metadata(path).ok().map(|meta| Stamp::of(&meta))
    }
}

/// What the io thread hands the render thread (`Source::arrive`).
pub enum Arrival {
    /// The file's text, read for the first time: the chunks to install.
    Loaded(Vec<Arc<Chunk>>),
    /// Another program changed the file: in the text at `version`, characters `low .. high` became `text`.
    Changed { version: u64, low: usize, high: usize, text: String, stamp: Stamp },
}

enum Task {
    Load,
    /// The text of a version that is no file (`File::fill`): its bytes, or why there are none.
    Fill(Vec<u8>, Option<String>),
    /// The text was edited: chunks `at .. at + gone` of the copy became `fresh`. `disk`: the edit
    /// is the file's own change taken by the text (`Arrival::Changed`), so nothing is to write.
    Edit { version: u64, at: usize, gone: usize, fresh: Vec<Arc<Chunk>>, disk: Option<Stamp> },
    /// The file may have changed. `again`: the text moved on under the change last handed over.
    Check { again: bool },
}

#[derive(Default)]
struct Desk {
    arrived: Option<Arrival>,
    /// The file's text was read; `reading`: loaded texts whose facts are still being read.
    loaded: bool,
    reading: u32,
    /// Why the file is not written, until it was said (`Source::arrive`).
    error: Option<String>,
    /// How the frame loop is told that something arrived.
    wake: Option<Waker>,
}

/// The io thread's side of a file.
#[derive(Default)]
struct Disk {
    /// The text as of the last edit taken: what is written, and what a changed file is compared with.
    chunks: Vec<Arc<Chunk>>,
    version: u64,
    /// The file as last read into the text or written from it; None: there is none.
    seen: Option<Stamp>,
    /// The state whose change was handed over and not taken yet.
    posted: Option<Stamp>,
    bom: bool,
    /// The file is no text this layer writes (`pull`).
    fixed: bool,
    /// Edited since it was written.
    dirty: bool,
}

pub struct File {
    pub path: PathBuf,
    /// Files that hold the text compiled, removed after every write: a compiled copy is told
    /// from a newer source by its second and its size, and both can be the same.
    stale: Vec<PathBuf>,
    /// The source's desk: where the first reading goes.
    shared: Arc<Shared>,
    /// Something is on the desk for the render thread: what `arrive` looks at, without a lock.
    pub waiting: AtomicBool,
    desk: Mutex<Desk>,
    bell: Condvar,
    disk: Mutex<Disk>,
}

struct Queue {
    tasks: Vec<(Arc<File>, Task)>,
    running: bool,
    working: bool,
}

struct Io {
    queue: Mutex<Queue>,
    /// Rung for the thread when a task waits, and for `flush` when none is left.
    bell: Condvar,
    rest: Condvar,
}

static IO: Io = Io { queue: Mutex::new(Queue { tasks: Vec::new(), running: false, working: false }), bell: Condvar::new(), rest: Condvar::new() };

/// A thread that died must not leave `flush` waiting at exit.
struct Shift;

impl Drop for Shift {
    fn drop(&mut self) {
        let mut queue = IO.queue.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        (queue.running, queue.working) = (false, false);
        IO.rest.notify_all();
    }
}

fn work() {
    let _shift = Shift;
    let mut dirty: Vec<Arc<File>> = vec![];
    loop {
        let tasks = {
            let mut queue = IO.queue.lock().unwrap();
            queue.working = false;
            IO.rest.notify_all();
            while queue.tasks.is_empty() { queue = IO.bell.wait(queue).unwrap(); }
            queue.working = true;
            std::mem::take(&mut queue.tasks)
        };
        for (file, task) in tasks {
            // A task that panics is said and the next one is done: the thread serves every file.
            let done = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| match task {
                Task::Load => file.install(pull(&file.path)),
                Task::Fill(bytes, error) => {
                    let pulled = decode(bytes, None);
                    file.install(Pulled { error: error.or(pulled.error), ..pulled });
                }
                Task::Edit { version, at, gone, fresh, disk } => {
                    if file.edit(version, at, gone, fresh, disk) { dirty.push(file.clone()); }
                }
                Task::Check { again } => file.check(again),
            }));
            if done.is_err() { file.fail("its io failed (see the panic above)".into()); }
        }
        // Every edit of this turn is in the copies: one write per file, of its newest text.
        for file in dirty.drain(..) {
            if std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| file.push())).is_err() {
                file.fail("its write failed (see the panic above)".into());
            }
        }
    }
}

/// The thread that reads the facts of loaded texts, one after another: what it is sent.
static READS: Mutex<Option<std::sync::mpsc::Sender<(Arc<File>, Vec<Arc<Chunk>>)>>> = Mutex::new(None);

/// Read the facts of a loaded text, with the tree, off the io thread: the frame that takes the
/// text finds them in the source's desk, or is woken when they are.
fn reader(file: &Arc<File>, chunks: Vec<Arc<Chunk>>) {
    let mut reads = READS.lock().unwrap();
    let sender = reads.get_or_insert_with(|| {
        let (sender, jobs) = std::sync::mpsc::channel::<(Arc<File>, Vec<Arc<Chunk>>)>();
        std::thread::Builder::new().name("melty-file-read".into()).spawn(move || {
            for (file, chunks) in jobs {
                // (Compound statements fold in it, the editor's default: a session asks for its own reading.)
                let job = Job { chunks, version: 1, blocks: true, terms: vec![], folds: vec![], lint: None, sites: None,
                                tree: true, grown: None, wake: vec![], due: Instant::now() };
                let found = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| crate::read(job, &file.shared).0));
                match found {
                    Ok(found) => file.shared.desk.lock().unwrap().keep(found),
                    Err(_) => file.fail("its text could not be read for facts (see the panic above)".into()),
                }
                file.post(|desk| desk.reading -= 1);
            }
        }).unwrap();
        sender
    });
    let _ = sender.send((file.clone(), chunks));
}

/// Wait until every edit made so far is in its file (the exit hook, tests, measurements).
pub fn flush() {
    let mut queue = IO.queue.lock().unwrap();
    while queue.running && (queue.working || !queue.tasks.is_empty()) { queue = IO.rest.wait(queue).unwrap(); }
}

struct Pulled {
    text: String,
    stamp: Option<Stamp>,
    bom: bool,
    /// Why this is no text to write back.
    error: Option<String>,
}

/// Read the file. One that is not there is an empty text, which the first edit creates.
fn pull(path: &Path) -> Pulled {
    let mut file = match fs::File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Pulled { text: String::new(), stamp: None, bom: false, error: None },
        Err(error) => return Pulled { text: String::new(), stamp: None, bom: false, error: Some(format!("not read ({error})")) },
    };
    // (Taken before the bytes: a write that lands while they are read leaves a stamp that is checked again.)
    let stamp = file.metadata().ok().map(|meta| Stamp::of(&meta));
    let mut bytes = Vec::with_capacity(stamp.map_or(0, |stamp| stamp.size as usize) + 1);
    if let Err(error) = file.read_to_end(&mut bytes) {
        return Pulled { text: String::new(), stamp, bom: false, error: Some(format!("not read ({error})")) };
    }
    decode(bytes, stamp)
}

/// The text `bytes` are: UTF-8, with or without a byte order mark. Lever: other encodings.
fn decode(mut bytes: Vec<u8>, stamp: Option<Stamp>) -> Pulled {
    let bom = bytes.starts_with(BOM);
    if bom { bytes.drain(..BOM.len()); }
    match String::from_utf8(bytes) {
        Ok(text) => Pulled { text, stamp, bom, error: None },
        Err(error) => Pulled { text: String::from_utf8_lossy(error.as_bytes()).into_owned(), stamp, bom,
                               error: Some("is no UTF-8: it is shown and never written".into()) },
    }
}

/// The text into the file, whole or not at all: a temp name beside it, renamed over it; the
/// file keeps its mode. Returns the stamp of what was written. No fsync: what goes with the
/// power is one version of a text whose next one is an edit away. Lever: `sync_data` before the rename.
fn write(path: &Path, chunks: &[Arc<Chunk>], bom: bool) -> std::io::Result<Stamp> {
    let mut name = path.as_os_str().to_owned();
    name.push(format!(".{}.tmp", std::process::id()));
    let temporary = PathBuf::from(name);
    let done = (|| {
        let file = fs::File::create(&temporary)?;
        if let Ok(old) = fs::metadata(path) {
            if old.permissions().readonly() { return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied, "the file is read-only")); }
            file.set_permissions(old.permissions())?;
        }
        let mut file = BufWriter::with_capacity(1 << 20, file);
        if bom { file.write_all(BOM)?; }
        for chunk in chunks { file.write_all(chunk.text.as_bytes())?; }
        let file = file.into_inner().map_err(|error| error.into_error())?;
        let stamp = Stamp::of(&file.metadata()?);
        fs::rename(&temporary, path)?;
        Ok(stamp)
    })();
    if done.is_err() { let _ = fs::remove_file(&temporary); }
    done
}

/// What turns the text of `chunks` into `new`: characters `low .. high` of the old text become
/// `new[from .. to]` (bytes): (low, high, from, to). None when they are the same text. What both
/// start and end with is skipped by comparing bytes; nothing is hashed.
pub fn differ(chunks: &[Arc<Chunk>], new: &str) -> Option<(usize, usize, usize, usize)> {
    let fresh = new.as_bytes();
    let size: usize = chunks.iter().map(|chunk| chunk.text.len()).sum();
    let (mut head, mut low) = (0, 0);           // what both start with: bytes, characters
    for chunk in chunks {
        let (old, rest) = (chunk.text.as_bytes(), &fresh[head..]);
        if rest.len() >= old.len() && rest[..old.len()] == *old {
            head += old.len();
            low += chunk.chars as usize;
            continue;
        }
        let mut same = old.iter().zip(rest).take_while(|(a, b)| a == b).count();
        while !chunk.text.is_char_boundary(same) { same -= 1; }
        head += same;
        low += chunk.text[..same].chars().count();
        break;
    }
    if head == size && head == fresh.len() { return None; }
    let most = size.min(fresh.len()) - head;
    let (mut tail, mut after) = (0, 0);         // what both end with, outside `head`
    for chunk in chunks.iter().rev() {
        let (old, rest, room) = (chunk.text.as_bytes(), &fresh[..fresh.len() - tail], most - tail);
        if room >= old.len() && rest[rest.len() - old.len()..] == *old {
            tail += old.len();
            after += chunk.chars as usize;
            continue;
        }
        let mut same = old.iter().rev().zip(rest.iter().rev()).take(room).take_while(|(a, b)| a == b).count();
        while !chunk.text.is_char_boundary(old.len() - same) { same -= 1; }
        tail += same;
        after += chunk.text[old.len() - same..].chars().count();
        break;
    }
    let chars: usize = chunks.iter().map(|chunk| chunk.chars as usize).sum();
    Some((low, chars - after, head, fresh.len() - tail))
}

impl File {
    /// The file at `path` (resolved by the caller) for the source whose desk is `shared`: watched
    /// from here on and loaded on the io thread. Without a path: a text that is no file, which
    /// `fill` gives and nothing writes (a version of the code, model/source_version_model.py).
    pub(crate) fn open(path: Option<PathBuf>, stale: Vec<PathBuf>, shared: Arc<Shared>) -> Arc<File> {
        let fixed = path.is_none();
        let file = Arc::new(File { path: path.unwrap_or_default(), stale, shared, waiting: AtomicBool::new(false), desk: Mutex::default(),
                                   bell: Condvar::new(), disk: Mutex::new(Disk { fixed, ..Disk::default() }) });
        if !fixed {
            watch::add(&file);
            file.task(Task::Load);
        }
        file
    }

    /// The text of a source opened without a path: cut and read on the io thread like a file's.
    /// `error`: why there is no text, said where the text is taken.
    pub fn fill(self: &Arc<File>, bytes: Vec<u8>, error: Option<String>) {
        self.task(Task::Fill(bytes, error));
    }

    fn task(self: &Arc<File>, task: Task) {
        let mut queue = IO.queue.lock().unwrap();
        queue.tasks.push((self.clone(), task));
        if !queue.running {
            queue.running = true;
            std::thread::Builder::new().name("melty-file".into()).spawn(work).unwrap();
        }
        // (A thread at work takes the task with its next turn: no call into the kernel for it.)
        if !queue.working { IO.bell.notify_one(); }
    }

    /// The text was edited (render thread): the io thread's copy follows, and the file after it.
    pub fn edited(self: &Arc<File>, version: u64, at: usize, gone: usize, fresh: Vec<Arc<Chunk>>, disk: Option<Stamp>) {
        self.task(Task::Edit { version, at, gone, fresh, disk });
    }

    /// The text moved on under the change last handed over: compare the file with it again.
    pub fn recheck(self: &Arc<File>) {
        self.task(Task::Check { again: true });
    }

    /// Put something on the desk and tell the render thread: whoever waits (`wait`), and the frame loop.
    fn post(&self, put: impl FnOnce(&mut Desk)) {
        let mut desk = self.desk.lock().unwrap();
        put(&mut desk);
        self.waiting.store(desk.arrived.is_some() || desk.error.is_some(), Ordering::Release);
        self.bell.notify_all();
        // Nothing here touches the interpreter: a wake is a token queued for the frame loop.
        if let Some(waker) = &desk.wake { unsafe { (waker.wake)(waker.data, waker.token) } }
    }

    fn fail(&self, error: String) {
        self.post(|desk| desk.error = Some(error));
    }

    /// The text arrived (a file read, a version filled): cut it, hand it over, read its facts.
    fn install(self: &Arc<File>, pulled: Pulled) {
        let mut chunks = store::chunks(&pulled.text);
        if chunks.is_empty() { chunks.push(Chunk::new("")); }
        let fixed = pulled.error.is_some() || self.path.as_os_str().is_empty();
        *self.disk.lock().unwrap() = Disk { chunks: chunks.clone(), version: 1, seen: pulled.stamp, posted: None, bom: pulled.bom,
                                           fixed, dirty: false };
        self.post(|desk| {
            (desk.arrived, desk.loaded) = (Some(Arrival::Loaded(chunks.clone())), true);
            desk.reading += 1;
            if pulled.error.is_some() { desk.error = pulled.error; }
        });
        reader(self, chunks);
    }

    /// The copy follows an edit. Returns whether the file has to be written for it and was not waiting already.
    fn edit(&self, version: u64, at: usize, gone: usize, fresh: Vec<Arc<Chunk>>, stamp: Option<Stamp>) -> bool {
        let mut disk = self.disk.lock().unwrap();
        disk.chunks.splice(at..at + gone, fresh);
        disk.version = version;
        match stamp {
            // The file's own change, now in the text: the two agree as of that state of the file.
            Some(stamp) => {
                disk.seen = Some(stamp);
                false
            }
            None => !std::mem::replace(&mut disk.dirty, true),
        }
    }

    /// Write the text when the file is still the one last seen; a file someone else wrote is read instead.
    fn push(self: &Arc<File>) {
        let mut disk = self.disk.lock().unwrap();
        disk.dirty = false;
        if disk.fixed { return; }
        let now = Stamp::at(&self.path);
        if now != disk.seen {
            if now.is_some() {
                drop(disk);
                return self.check(false);
            }
            disk.seen = None;       // deleted: the edit puts it back
        }
        match write(&self.path, &disk.chunks, disk.bom) {
            Ok(stamp) => {
                disk.seen = Some(stamp);
                for compiled in &self.stale { let _ = fs::remove_file(compiled); }
            }
            Err(error) => {
                drop(disk);
                self.fail(format!("not written ({error})"));
            }
        }
    }

    /// The file may have changed: when its stamp is not the one last seen it is read, and what
    /// differs from the copy is handed to the render thread as one splice.
    fn check(self: &Arc<File>, again: bool) {
        let mut disk = self.disk.lock().unwrap();
        let Some(now) = Stamp::at(&self.path) else { return };
        if Some(now) == disk.seen || (!again && Some(now) == disk.posted) { return; }
        // (A text nobody installed yet is simply loaded again.)
        if matches!(self.desk.lock().unwrap().arrived, Some(Arrival::Loaded(_))) {
            drop(disk);
            return self.install(pull(&self.path));
        }
        let pulled = pull(&self.path);
        let (Some(stamp), None) = (pulled.stamp, &pulled.error) else {
            drop(disk);
            return self.fail(pulled.error.unwrap_or_else(|| "changed and could not be read".into()));
        };
        disk.bom = pulled.bom;
        match differ(&disk.chunks, &pulled.text) {
            None => disk.seen = Some(stamp),        // touched, or written as it was
            Some((low, high, from, to)) => {
                disk.posted = Some(stamp);
                let arrival = Arrival::Changed { version: disk.version, low, high, text: pulled.text[from..to].to_string(), stamp };
                drop(disk);
                self.post(|desk| desk.arrived = Some(arrival));
            }
        }
    }

    /// Render thread: what arrived, once, and why the file is not written (once too).
    pub fn take(&self) -> (Option<Arrival>, Option<String>) {
        let mut desk = self.desk.lock().unwrap();
        self.waiting.store(false, Ordering::Release);
        (desk.arrived.take(), desk.error.take())
    }

    /// Wait for the file's text and, with `reading`, for its first reading (any thread but a frame's).
    pub fn wait(&self, reading: bool) {
        let mut desk = self.desk.lock().unwrap();
        while !(desk.loaded && (desk.reading == 0 || !reading)) { desk = self.bell.wait(desk).unwrap(); }
    }

    /// Wake the frame loop through `waker` when something arrives. Returns whether something waits already.
    pub(crate) fn heed(&self, waker: Waker) -> bool {
        self.desk.lock().unwrap().wake = Some(waker);
        self.waiting.load(Ordering::Acquire)
    }
}

/// Changes made by other programs, as events: one inotify instance for every file, one watch
/// per folder. The thread only names the file an event is about; the io thread looks.
#[cfg(target_os = "linux")]
mod watch {
    use std::collections::HashMap;
    use std::ffi::{c_char, CString, OsString};
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    use std::path::PathBuf;
    use std::sync::{Arc, Mutex, Weak};

    use super::{File, Task};

    extern "C" {
        fn inotify_init1(flags: i32) -> i32;
        fn inotify_add_watch(fd: i32, path: *const c_char, mask: u32) -> i32;
        fn read(fd: i32, buffer: *mut u8, count: usize) -> isize;
    }

    const CLOEXEC: i32 = 0o2000000;
    const CLOSE_WRITE: u32 = 0x8;
    const MOVED_TO: u32 = 0x80;
    const OVERFLOW: u32 = 0x4000;
    /// Lever: the events that mean a file has new contents: a writer closed it, or a file was
    /// renamed onto it (how editors, git and this layer save). Not every `write` call of a
    /// writer: a file is read when it is whole.
    const MASK: u32 = CLOSE_WRITE | MOVED_TO;

    struct Watch {
        fd: i32,
        folders: HashMap<PathBuf, i32>,
        files: HashMap<(i32, OsString), Weak<File>>,
    }

    static WATCH: Mutex<Option<Watch>> = Mutex::new(None);

    pub fn add(file: &Arc<File>) {
        let (Some(folder), Some(name)) = (file.path.parent(), file.path.file_name()) else { return };
        let mut held = WATCH.lock().unwrap();
        if held.is_none() {
            let fd = unsafe { inotify_init1(CLOEXEC) };
            if fd < 0 {
                eprintln!("meltygui: Source cannot watch files ({}): changes by other programs are not followed", std::io::Error::last_os_error());
                return;
            }
            *held = Some(Watch { fd, folders: HashMap::new(), files: HashMap::new() });
            std::thread::Builder::new().name("melty-file-watch".into()).spawn(move || listen(fd)).unwrap();
        }
        let watch = held.as_mut().unwrap();
        let wd = match watch.folders.get(folder) {
            Some(&wd) => wd,
            None => {
                let Ok(path) = CString::new(folder.as_os_str().as_bytes()) else { return };
                let wd = unsafe { inotify_add_watch(watch.fd, path.as_ptr(), MASK) };
                if wd < 0 {
                    eprintln!("meltygui: Source cannot watch {} ({})", folder.display(), std::io::Error::last_os_error());
                    return;
                }
                watch.folders.insert(folder.to_owned(), wd);
                wd
            }
        };
        watch.files.retain(|_, held| held.strong_count() > 0);
        watch.files.insert((wd, name.to_owned()), Arc::downgrade(file));
    }

    fn listen(fd: i32) {
        // (Events are read by their bytes: a header of four 32 bit fields, then the name.)
        let mut buffer = [0u8; 8192];
        loop {
            let size = unsafe { read(fd, buffer.as_mut_ptr(), buffer.len()) };
            if size <= 0 {
                if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted { continue; }
                return;
            }
            let (size, mut at, mut found) = (size as usize, 0, vec![]);
            {
                let held = WATCH.lock().unwrap();
                let Some(watch) = held.as_ref() else { return };
                while at + 16 <= size {
                    let field = |from: usize| u32::from_ne_bytes(buffer[at + from..at + from + 4].try_into().unwrap());
                    let (wd, mask, length) = (field(0) as i32, field(4), field(12) as usize);
                    let name = &buffer[at + 16..(at + 16 + length).min(size)];
                    let name = OsString::from_vec(name[..name.iter().position(|&byte| byte == 0).unwrap_or(name.len())].to_vec());
                    at += 16 + length;
                    if mask & OVERFLOW != 0 {
                        // Events were dropped: every file is looked at.
                        found.extend(watch.files.values().filter_map(Weak::upgrade));
                    } else if let Some(file) = watch.files.get(&(wd, name)).and_then(Weak::upgrade) {
                        found.push(file);
                    }
                }
            }
            for file in found { file.task(Task::Check { again: false }); }
        }
    }
}

#[cfg(not(target_os = "linux"))]
mod watch {
    /// Not built: changes by other programs are not followed on this platform.
    pub fn add(_: &std::sync::Arc<super::File>) {}
}
