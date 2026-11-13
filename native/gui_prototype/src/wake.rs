//! Wakes from threads that never take the interpreter lock.
//!
//! `RetainedCache.wake(node)` is Python: a worker thread that holds no
//! interpreter (a Rust reading thread) cannot call it. `Wakes` is the queue
//! such a thread writes to instead: it pushes a token and pokes the event
//! wait, both without Python; the frame loop drains the tokens where it
//! wakes up and hands each to its cache (`glfw_utils.drain_wakes`), so from
//! there on a wake is the ordinary `cache.wake(node)`.
//!
//! Another extension reaches it by C ABI through the capsule `handle()`
//! returns (`Handle`: copy it, `keep` the data, `free` it when done).
use std::ffi::{c_void, CString};
use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use pyo3::types::PyCapsule;

/// How the event wait is woken: a C function to call (GLFW's
/// `glfwPostEmptyEvent`) or a descriptor to write a byte to (the Wayland
/// backend's wake socket). None: the loop notices at its next turn.
enum Poke {
    None,
    Call(unsafe extern "C" fn()),
    Fd(i32),
}

struct Inner {
    tokens: Mutex<Vec<u64>>,
    /// Held while poking: once `poke(None)` returned, no thread is inside the window system.
    poke: Mutex<Poke>,
}

/// The C ABI of a wake: `wake(data, token)` from any thread, without the
/// interpreter. `keep(data)` and `free(data)` count who holds `data`.
#[repr(C)]
pub struct Handle {
    wake: unsafe extern "C" fn(*const c_void, u64),
    keep: unsafe extern "C" fn(*const c_void),
    free: unsafe extern "C" fn(*const c_void),
    data: *const c_void,
}

unsafe impl Send for Handle {}

#[cfg(unix)]
extern "C" {
    fn write(fd: i32, buffer: *const u8, count: usize) -> isize;
}

unsafe extern "C" fn wake(data: *const c_void, token: u64) {
    let inner = &*(data as *const Inner);
    {
        let mut tokens = inner.tokens.lock().unwrap();
        if !tokens.contains(&token) { tokens.push(token); }
    }
    match *inner.poke.lock().unwrap() {
        Poke::Call(call) => call(),
        #[cfg(unix)]
        Poke::Fd(fd) => { write(fd, b"1".as_ptr(), 1); }
        _ => {}
    }
}

unsafe extern "C" fn keep(data: *const c_void) {
    Arc::increment_strong_count(data as *const Inner);
}

unsafe extern "C" fn free(data: *const c_void) {
    Arc::decrement_strong_count(data as *const Inner);
}

#[pyclass(module = "meltygui.core.rendering._gui_native")]
pub struct Wakes {
    inner: Arc<Inner>,
}

#[pymethods]
impl Wakes {
    #[new]
    fn new() -> Wakes {
        Wakes { inner: Arc::new(Inner { tokens: Mutex::new(vec![]), poke: Mutex::new(Poke::None) }) }
    }

    /// How a wake rouses the event wait: `("call", address)` of a C function
    /// taking nothing, `("fd", descriptor)` to write a byte to, or None.
    /// Returns once no thread is using the one before: clear it before the
    /// window system goes away.
    #[pyo3(signature = (kind=None, value=0))]
    fn poke(&self, kind: Option<&str>, value: usize) {
        *self.inner.poke.lock().unwrap() = match kind {
            Some("call") if value != 0 => Poke::Call(unsafe { std::mem::transmute::<usize, unsafe extern "C" fn()>(value) }),
            Some("fd") => Poke::Fd(value as i32),
            _ => Poke::None,
        };
    }

    /// The capsule another extension wakes through (see the module text).
    fn handle<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyCapsule>> {
        let data = Arc::into_raw(self.inner.clone()) as *const c_void;
        PyCapsule::new_with_destructor(py, Handle { wake, keep, free, data }, Some(CString::new("meltygui.wake").unwrap()),
                                       |handle: Handle, _| unsafe { (handle.free)(handle.data) })
    }

    /// Queue `token` as a thread without the interpreter would (tests).
    fn wake(&self, token: u64) {
        unsafe { wake(Arc::as_ptr(&self.inner) as *const c_void, token) }
    }

    /// The tokens queued since the last call, each once: for the frame loop.
    fn take(&self) -> Vec<u64> {
        std::mem::take(&mut *self.inner.tokens.lock().unwrap())
    }
}
