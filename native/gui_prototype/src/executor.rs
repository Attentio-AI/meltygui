//! One retained view execution in a single native call.
//!
//! The bridge keeps its dictionaries authoritative. This executor holds
//! references to them and a small record per view, runs the straight-line
//! path natively (capture scope, body, measurement, packet copy, commit) and
//! calls back into the bridge only where something actually happens: layout
//! transactions, live input, tints, retirement, resizes, hit regions, errors.
use crate::hash::Map as HashMap;
use crate::retained::RetainedGraph;
use crate::texture::{DrawBuffers, TextureCache};
use crate::{Capture, NativeState, Renderer, Value, SLOT_CONTENT_HEIGHT, SLOT_HEIGHT, SLOT_MEASURED_WIDTH};
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyKeyError, PyRuntimeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PySet, PyTuple};
use std::rc::Rc;

// Bridge-owned objects, fixed for the cache's lifetime.
struct Shared {
    owner: Py<PyAny>,
    graph: Py<RetainedGraph>,
    graphics: bool,
    input: Py<PyAny>,
    stack: Py<PyList>,
    events: Py<PyDict>,
    backgrounds: Py<PyDict>,
    texture_refs: Py<PyDict>,
    staged_regions: Py<PyDict>,
    regions: Py<PyDict>,
    capture_info: Py<PyDict>,
    // The layout tables' mutation counter: nonzero once any layout existed.
    changes: Py<PyList>,
    right_active: Py<PySet>,
    pointer_claims: Py<PyDict>,
    active: Py<PySet>,
    no_claim: Py<PyAny>,
    clear: Py<PyAny>,
}

struct Record {
    renderer: Py<Renderer>,
    func: Py<PyAny>,
    code: Option<Py<PyAny>>,
    unique: u128,
    label: Py<PyBytes>,
    state: Py<NativeState>,
    caller_keys: Py<PyAny>,
    // The node as a Python int, for the bridge's dictionaries.
    key: Py<PyAny>,
    scope: Option<Py<PyAny>>,
    // Mirrors of what the bridge's dictionaries already hold for this view, so
    // an identical write is skipped: a committed capture with a transparent
    // background, and an idle pointer claim.
    captured: bool,
    clear_background: bool,
    idle_claim: bool,
}
impl Record {
    fn snapshot(&self, py: Python<'_>) -> Self {
        Self {
            renderer: self.renderer.clone_ref(py), func: self.func.clone_ref(py),
            code: self.code.as_ref().map(|code| code.clone_ref(py)), unique: self.unique,
            label: self.label.clone_ref(py), state: self.state.clone_ref(py),
            caller_keys: self.caller_keys.clone_ref(py), key: self.key.clone_ref(py),
            scope: self.scope.as_ref().map(|scope| scope.clone_ref(py)),
            captured: self.captured, clear_background: self.clear_background, idle_claim: self.idle_claim,
        }
    }
}

// What the caller still has to undo or release after an execution.
#[derive(Default)]
struct Progress<'py> {
    began: Option<Bound<'py, PyAny>>,
    keepalive: Option<Bound<'py, PyList>>,
    reference_start: usize,
}

#[pyclass(unsendable, module = "meltygui.core.rendering._gui_native")]
pub struct Executor {
    shared: Option<Rc<Shared>>,
    gpu: Option<Py<TextureCache>>,
    keepalive: Option<Py<PyList>>,
    records: HashMap<u64, Record>,
    runs: u64,
    // The cache's presentation origin and whether cheap views share one pass.
    origin: (f64, f64),
    auto_cache: bool,
}

impl Executor {
    fn update(slf: &Bound<'_, Self>, node: u64, change: impl FnOnce(&mut Record)) {
        if let Some(record) = slf.borrow_mut().records.get_mut(&node) { change(record); }
    }
    #[allow(clippy::too_many_arguments)]
    fn execute<'py>(slf: &Bound<'py, Self>, py: Python<'py>, shared: &Shared, record: &Record, node: u64,
                    independent: bool, origin: (f64, f64), auto_cache: bool, mouse_owner: Option<u64>,
                    focus: Option<u64>, events: &Bound<'py, PyDict>, progress: &mut Progress<'py>) -> PyResult<()> {
        let owner = shared.owner.bind(py);
        let graph = shared.graph.bind(py);
        let key = record.key.bind(py);
        let layout = || -> PyResult<bool> { Ok(shared.changes.bind(py).get_item(0)?.extract::<u64>()? != 0) };
        // The live input, the declaration's arguments (shared, read only), the
        // rectangle before the geometry sync, origin, owner facts and scroll.
        let (input, arguments, old_rect, mut local, parent, portal, auto_height, scroll) =
            graph.borrow_mut().begin_execution(py, node)?;
        let arguments = arguments.into_bound(py);
        let w = old_rect.2;
        let requested_h = if auto_height { -1. } else { old_rect.3 };
        // Measure without the old texture's clip truncating newly grown content.
        // This is a logical capture viewport, not an 8192px texture allocation.
        let h = if auto_height { 8192. } else { requested_h.max(1.) };
        // Layout transactions exist once any row, column or window was
        // declared; until then an owner opens one lazily on first use.
        if layout()? && owner.call_method1(intern!(py, "_layout_begin"), (node,))?.is_truthy()? {
            local = graph.borrow_mut().origin(py, node, 0, false)?;
        }
        if shared.graphics {
            if slf.borrow().gpu.is_none() {
                let gpu = owner.call_method0(intern!(py, "_ensure_gpu"))?.downcast_into::<TextureCache>()?;
                slf.borrow_mut().gpu = Some(gpu.unbind());
            }
            let scope = match &record.scope {
                Some(scope) => scope.bind(py).clone(),
                None => {
                    let scope = owner.call_method1(intern!(py, "_capture"), (node,))?;
                    let held = scope.clone().unbind();
                    Self::update(slf, node, |record| record.scope = Some(held));
                    scope
                }
            };
            let held = slf.borrow().keepalive.as_ref().map(|list| list.bind(py).clone());
            let keepalive = match held {
                Some(list) => list,
                None => {
                    // One list for the binding's lifetime; new frames clear it in place.
                    let list = owner.call_method0(intern!(py, "_keepalive_list"))?.downcast_into::<PyList>()?;
                    slf.borrow_mut().keepalive = Some(list.clone().unbind());
                    list
                }
            };
            progress.reference_start = keepalive.len();
            progress.keepalive = Some(keepalive);
            // The capture scope, the view's ID scope and its measuring group.
            scope.call_method1(intern!(py, "begin_view"),
                               (w, h, origin.0 + local.0, origin.1 + local.1, record.label.bind(py)))?;
            progress.began = Some(scope);
        }
        let body = record.renderer.borrow(py).invoke(py, Some(input.clone_ref(py)), Some(&arguments), Some(Capture {
            unique: record.unique, explicit: record.caller_keys.bind(py), rect: old_rect, auto_height, scroll,
            cache: owner, events: events.as_any(), fused: shared.graphics,
        }));
        let result = match body {
            Ok(result) => result.into_bound(py).downcast_into::<PyTuple>()?,
            Err(error) => {
                if let Some(scope) = &progress.began {
                    // Settle input ownership, then unwind whatever the body left open.
                    let finish = shared.input.bind(py).call_method1(intern!(py, "finish"), (node, scope));
                    scope.call_method0(intern!(py, "end"))?;
                    finish?;
                }
                return Err(error);
            }
        };
        let state = result.get_item(2)?.downcast_into::<NativeState>()?;
        let tinted = result.get_item(3)?.is_truthy()?;
        let mut content_height = None;
        let mut buffers: Vec<DrawBuffers> = Vec::new();
        let mut idle_claim = record.idle_claim;
        if let Some(scope) = &progress.began {
            // A view that owns no live widget, pointer or focus claims nothing
            // and cannot tick: its capture ends with the measurement.
            let idle = mouse_owner != Some(node) && focus != Some(node);
            let ended = scope.call_method1(intern!(py, "end_view"), (idle,))?.downcast_into::<PyTuple>()?;
            let (measured_w, measured_h): (f64, f64) = (ended.get_item(0)?.extract()?, ended.get_item(1)?.extract()?);
            {
                let mut ds = state.borrow_mut();
                ds.put_slot(SLOT_CONTENT_HEIGHT, Value::Float(measured_h));
                ds.put_slot(SLOT_MEASURED_WIDTH, Value::Float(measured_w));
            }
            let listed = ended.get_item(3)?;
            let listed = if listed.is_none() {
                // Read item and input state while the capture is still open.
                let finish = shared.input.bind(py).call_method1(intern!(py, "finish"), (node, scope));
                scope.call_method0(intern!(py, "end"))?;
                finish?;
                idle_claim = false;
                scope.call_method0(intern!(py, "buffers"))?
            } else {
                if shared.right_active.bind(py).len() > 0 { shared.right_active.bind(py).discard(key)?; }
                if !idle_claim {
                    shared.pointer_claims.bind(py).set_item(key, shared.no_claim.bind(py))?;
                    idle_claim = true;
                }
                if shared.active.bind(py).len() > 0 { shared.active.bind(py).discard(key)?; }
                listed
            };
            buffers = listed.extract()?;
            content_height = Some(measured_h);
        }
        // A declaration has state before its first execution, including culled
        // views. Register that first alias before skipping repeats.
        if !state.is(record.state.bind(py)) {
            owner.call_method1(intern!(py, "_state_replaced"), (node, &state))?;
        } else if !record.captured {
            owner.call_method1(intern!(py, "_first_capture"), (node, &state))?;
        }
        let mut measured_h = if !auto_height { requested_h } else {
            match content_height {
                Some(height) => height,
                None => state.borrow().at(SLOT_HEIGHT).and_then(Value::number).unwrap_or(0.),
            }
        };
        if !(0.0..=8192.0).contains(&measured_h) {
            return Err(PyValueError::new_err("measured cache height must be 0..8192 pixels"));
        }
        let bound = |name: &Bound<'py, pyo3::types::PyString>| -> PyResult<f64> {
            arguments.get_item(name)?.ok_or_else(|| PyKeyError::new_err(name.clone().unbind()))?.extract()
        };
        let (minimum, maximum) = (bound(intern!(py, "min_height"))?, bound(intern!(py, "max_height"))?);
        if measured_h < minimum { measured_h = minimum; }
        if measured_h > maximum { measured_h = maximum; }
        // Graph measurement writes resolved size without authoring an override.
        let background = if tinted || arguments.contains(intern!(py, "tint"))? {
            Some(owner.call_method1(intern!(py, "_background"), (&state, &arguments))?)
        } else { None };
        let mut size = 0_usize;
        if shared.graphics {
            // Copy the finalized draw buffers in place. Retained commands
            // outlive this ImGui frame, and a texture-ID object may itself own
            // its resource, so the bridge keeps those until packet replacement.
            let gpu = slf.borrow().gpu.as_ref().unwrap().clone_ref(py);
            let (bytes, textures) = gpu.borrow_mut(py).capture(py, node, w.ceil() as i32, (measured_h.ceil() as i32).max(1),
                                                                 auto_cache, parent == 0 || portal, buffers)?;
            size = bytes;
            shared.texture_refs.bind(py).set_item(key, textures)?;
        }
        let (resized, retired, parent, residency) =
            graph.borrow_mut().finish_execution(py, node, &result, independent, w, measured_h)?;
        if retired { owner.call_method0(intern!(py, "_retire"))?; }
        let clear = background.is_none();
        if !(record.captured && record.clear_background && clear) {
            match &background {
                Some(color) => shared.backgrounds.bind(py).set_item(key, color)?,
                None => shared.backgrounds.bind(py).set_item(key, shared.clear.bind(py))?,
            }
        }
        // Hit regions staged by the body replace the previous execution's.
        if shared.staged_regions.bind(py).len() > 0 || shared.regions.bind(py).len() > 0 {
            owner.call_method1(intern!(py, "_commit_regions"), (node,))?;
        }
        // Solver follow-up, the resize log and the owner's layout commit.
        if resized || layout()? {
            owner.call_method1(intern!(py, "_layout_end"), (node, resized, old_rect.2, old_rect.3, w, measured_h))?;
        }
        if shared.graphics {
            let residency = (residency != 0).then_some(residency);
            let current = shared.capture_info.bind(py).get_item(key)?
                .and_then(|info| info.extract::<(u64, usize, Option<u64>)>().ok());
            if current != Some((parent, size.max(1), residency)) {
                owner.call_method1(intern!(py, "_track_capture"), (node, parent, residency, size))?;
            }
        }
        {
            let mut this = slf.borrow_mut();
            // The retained hit geometry may have changed; pointer samples compare this.
            this.runs += 1;
            if let Some(record) = this.records.get_mut(&node) {
                record.captured = true;
                record.clear_background = clear;
                record.idle_claim = idle_claim;
                record.state = state.unbind();
            }
        }
        if result.get_item(0)?.is_truthy()? {
            owner.call_method1(intern!(py, "_propagate"), (node, input))?;
        }
        Ok(())
    }
}

impl Executor {
    /// Whether the view has a committed capture.
    pub(crate) fn captured(&self, node: u64) -> bool {
        self.records.get(&node).is_some_and(|record| record.captured)
    }
    pub(crate) fn run_node(slf: &Bound<'_, Self>, py: Python<'_>, node: u64, independent: bool) -> PyResult<()> {
        let (shared, mut record, origin, auto_cache) = {
            let this = slf.borrow();
            let shared = this.shared.clone().ok_or_else(|| PyRuntimeError::new_err("retained runtime is closed"))?;
            let record = this.records.get(&node).ok_or_else(|| PyRuntimeError::new_err("retired view"))?.snapshot(py);
            (shared, record, this.origin, this.auto_cache)
        };
        // The views currently owning the pointer and keyboard focus.
        let input = shared.input.bind(py);
        let mouse_owner: Option<u64> = input.getattr(intern!(py, "mouse_owner"))?.extract()?;
        let focus: Option<u64> = input.getattr(intern!(py, "focus"))?.extract()?;
        let owner = shared.owner.bind(py);
        // Source edits recompile the parameter plan before the body runs.
        let current = record.func.bind(py).getattr(intern!(py, "__code__"))?;
        if record.code.as_ref().map_or(true, |code| !current.is(code.bind(py))) {
            owner.call_method1(intern!(py, "_refresh"), (node,))?;
            let held = current.clone().unbind();
            Self::update(slf, node, |record| record.code = Some(held));
            record.code = Some(current.unbind());
        }
        let key = record.key.bind(py);
        let pending = shared.events.bind(py);
        let events = if pending.len() > 0 {
            let events = pending.get_item(key)?;
            if events.is_some() { pending.del_item(key)?; }
            events.map(|events| events.downcast_into::<PyDict>()).transpose()?
        } else { None };
        let events = events.unwrap_or_else(|| PyDict::new(py));
        let stack = shared.stack.bind(py);
        stack.append(key)?;
        let mut progress = Progress::default();
        let outcome = Self::execute(slf, py, &shared, &record, node, independent, origin, auto_cache, mouse_owner,
                                    focus, &events, &mut progress);
        // A failed body leaves no layout, declarations, regions or consumed events behind.
        let aborted = match &outcome {
            Err(_) => owner.call_method1(intern!(py, "_abort_execution"), (node, &events)).map(drop),
            Ok(()) => Ok(()),
        };
        let popped = stack.call_method0(intern!(py, "pop")).map(drop);
        let released = match (&progress.began, &progress.keepalive) {
            (Some(scope), Some(keepalive)) if keepalive.len() > progress.reference_start =>
                owner.call_method1(intern!(py, "_release_references"), (scope, progress.reference_start)).map(drop),
            _ => Ok(()),
        };
        aborted?;
        popped?;
        released?;
        outcome
    }
}

#[pymethods]
impl Executor {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(owner: Py<PyAny>, graph: Py<RetainedGraph>, graphics: bool, input: Py<PyAny>, stack: Py<PyList>,
           events: Py<PyDict>, backgrounds: Py<PyDict>, texture_refs: Py<PyDict>, staged_regions: Py<PyDict>,
           regions: Py<PyDict>, capture_info: Py<PyDict>, changes: Py<PyList>, right_active: Py<PySet>,
           pointer_claims: Py<PyDict>, active: Py<PySet>, no_claim: Py<PyAny>, clear: Py<PyAny>) -> Self {
        Self {
            shared: Some(Rc::new(Shared {
                owner, graph, graphics, input, stack, events, backgrounds, texture_refs, staged_regions, regions,
                capture_info, changes, right_active, pointer_claims, active, no_claim, clear,
            })),
            gpu: None,
            keepalive: None,
            records: HashMap::default(),
            runs: 0,
            origin: (0., 0.),
            auto_cache: true,
        }
    }
    /// The presentation origin captures are positioned against.
    fn set_origin(&mut self, origin: (f64, f64)) {
        self.origin = origin;
    }
    /// Whether cheap views keep a shared logical reference instead of a raster.
    fn set_auto_cache(&mut self, enabled: bool) {
        self.auto_cache = enabled;
    }
    /// Register a declared view: its native renderer, function, identity,
    /// state and the caller's explicit keys.
    #[allow(clippy::too_many_arguments)]
    fn attach(&mut self, py: Python<'_>, node: u64, renderer: Py<Renderer>, func: Py<PyAny>, unique: u128,
              state: Py<NativeState>, caller_keys: Py<PyAny>) -> PyResult<()> {
        let code = func.bind(py).getattr(intern!(py, "__code__")).ok().map(Bound::unbind);
        let previous = self.records.insert(node, Record {
            renderer, func, code, unique,
            label: PyBytes::new(py, format!("gui-{unique}").as_bytes()).unbind(),
            state, caller_keys, key: node.into_pyobject(py)?.into_any().unbind(),
            scope: None, captured: false, clear_background: false, idle_claim: false,
        });
        drop(previous);
        Ok(())
    }
    fn set_caller_keys(&mut self, node: u64, caller_keys: Py<PyAny>) {
        if let Some(record) = self.records.get_mut(&node) {
            let previous = std::mem::replace(&mut record.caller_keys, caller_keys);
            drop(previous);
        }
    }
    /// The view's capture was dropped: its scope, background and input claim
    /// are gone from the bridge's dictionaries.
    fn forget_capture(&mut self, node: u64) {
        if let Some(record) = self.records.get_mut(&node) {
            record.captured = false;
            record.clear_background = false;
            record.idle_claim = false;
            let scope = record.scope.take();
            drop(scope);
        }
    }
    /// The view retired.
    fn forget(&mut self, node: u64) {
        let record = self.records.remove(&node);
        drop(record);
    }
    /// Counts executions; a pointer sample taken before one is stale.
    #[getter]
    fn runs(&self) -> u64 {
        self.runs
    }
    fn clear(&mut self) {
        let records = std::mem::take(&mut self.records);
        let shared = self.shared.take();
        let gpu = self.gpu.take();
        let keepalive = self.keepalive.take();
        drop((records, shared, gpu, keepalive));
    }
    /// Execute one dirty view.
    fn run(slf: &Bound<'_, Self>, py: Python<'_>, node: u64, independent: bool) -> PyResult<()> {
        Self::run_node(slf, py, node, independent)
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(shared) = &self.shared {
            visit.call(&shared.owner)?;
            visit.call(&shared.graph)?;
            visit.call(&shared.input)?;
        }
        if let Some(gpu) = &self.gpu { visit.call(gpu)?; }
        for record in self.records.values() {
            visit.call(&record.renderer)?;
            visit.call(&record.func)?;
            visit.call(&record.state)?;
            visit.call(&record.caller_keys)?;
            if let Some(scope) = &record.scope { visit.call(scope)?; }
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.clear();
    }
}
