//! Repeated declarations in one native crossing.
//!
//! The bridge resolves a declaration fully the first time and remembers its
//! inputs and result here. A later call with the same keyword arguments, flow
//! position and state geometry is declared again natively. A child under an
//! executing parent finishes without returning to the bridge: unchanged, it
//! is only placed again; dirty, the executor runs it first. Anything else
//! hands the already declared node back for the bridge's tail.
use crate::executor::Executor;
use crate::hash::Map as HashMap;
use crate::identity::Identities;
use crate::retained::RetainedGraph;
use crate::texture::TextureCache;
use crate::NativeState;
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use std::rc::Rc;

struct Slot {
    // The caller's keyword arguments and position-default generation.
    kwargs: Py<PyDict>,
    generation: u64,
    // Layout cursor coordinates the resolution depended on; None: explicit axis.
    flow: (Option<f64>, Option<f64>),
    automatic_width: Option<f64>,
    // (arguments, x, y, w, h, automatic height, scrollable, extras, caller keys).
    resolved: Py<PyTuple>,
    arguments: Py<PyDict>,
    position: (f64, f64),
    rect: (f64, f64, f64, f64),
    scrollable: bool,
    extras: bool,
    state: Py<NativeState>,
    // The node and input of the latest declaration through this slot.
    node: u64,
    input: Option<Py<PyAny>>,
}

struct Api {
    cursor: Py<PyAny>,
    set_cursor: Py<PyAny>,
    dummy: Py<PyAny>,
    draw_list: Py<PyAny>,
    content_region: Py<PyAny>,
}

#[pyclass(unsendable, module = "meltygui.core.rendering._gui_native")]
pub struct Declarations {
    graph: Option<Py<RetainedGraph>>,
    identities: Option<Py<Identities>>,
    gpu: Option<Py<TextureCache>>,
    executor: Option<Py<Executor>>,
    api: Option<Rc<Api>>,
    path: Py<PyAny>,
    slots: HashMap<usize, Slot>,
}

#[pymethods]
impl Declarations {
    #[new]
    #[pyo3(signature = (graph, identities, imgui=None))]
    fn new(py: Python<'_>, graph: Py<RetainedGraph>, identities: Py<Identities>, imgui: Option<Bound<'_, PyAny>>) -> PyResult<Self> {
        let api = match imgui {
            Some(module) => Some(Rc::new(Api {
                cursor: module.getattr(intern!(py, "get_cursor_screen_pos"))?.unbind(),
                set_cursor: module.getattr(intern!(py, "set_cursor_screen_pos"))?.unbind(),
                dummy: module.getattr(intern!(py, "dummy"))?.unbind(),
                draw_list: module.getattr(intern!(py, "get_window_draw_list"))?.unbind(),
                content_region: module.getattr(intern!(py, "get_content_region_available"))?.unbind(),
            })),
            None => None,
        };
        Ok(Self {
            graph: Some(graph),
            identities: Some(identities),
            gpu: None,
            executor: None,
            api,
            path: py.import("pathlib")?.getattr("Path")?.unbind(),
            slots: HashMap::default(),
        })
    }
    /// The texture cache, once the bridge created it; needed to place the
    /// image of an unchanged child inside a non-scrolling parent.
    #[pyo3(signature = (gpu))]
    fn set_gpu(&mut self, gpu: Option<Py<TextureCache>>) {
        self.gpu = gpu;
    }
    /// Remember a fully resolved declaration. `resolved` is (arguments, x, y,
    /// w, h, automatic height, scrollable, extras, caller keys); `flow_x` and
    /// `flow_y` are the cursor coordinates it depended on, None when explicit.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (handle, kwargs, generation, flow_x, flow_y, automatic_width, resolved, state))]
    fn remember(&mut self, py: Python<'_>, handle: usize, kwargs: Py<PyDict>, generation: u64, flow_x: Option<f64>,
                flow_y: Option<f64>, automatic_width: Option<f64>, resolved: Bound<'_, PyTuple>,
                state: Py<NativeState>) -> PyResult<()> {
        let arguments = resolved.get_item(0)?.downcast_into::<PyDict>()?.unbind();
        let (x, y, w, h): (f64, f64, f64, f64) = (resolved.get_item(1)?.extract()?, resolved.get_item(2)?.extract()?,
                                                  resolved.get_item(3)?.extract()?, resolved.get_item(4)?.extract()?);
        let auto_height = resolved.get_item(5)?.is_truthy()?;
        state.borrow_mut(py).remember_geometry();
        // Replace in two steps: dropping the old slot can run Python destructors.
        let previous = self.slots.insert(handle, Slot {
            kwargs, generation, flow: (flow_x, flow_y), automatic_width,
            arguments, position: (x, y), rect: (x, y, w, if auto_height { -1. } else { h }),
            scrollable: resolved.get_item(6)?.is_truthy()?, extras: resolved.get_item(7)?.is_truthy()?,
            resolved: resolved.unbind(), state, node: 0, input: None,
        });
        drop(previous);
        Ok(())
    }
    /// Drop one remembered declaration, releasing its caller arguments.
    fn forget(&mut self, handle: usize) {
        let previous = self.slots.remove(&handle);
        drop(previous);
    }
    fn clear(&mut self) {
        let slots = std::mem::take(&mut self.slots);
        self.graph = None;
        self.identities = None;
        self.gpu = None;
        let executor = self.executor.take();
        drop((slots, executor));
    }
    /// The executor that runs a dirty child without returning to the bridge.
    #[pyo3(signature = (executor))]
    fn set_executor(&mut self, executor: Option<Py<Executor>>) {
        self.executor = executor;
    }
    /// Declare one view. Returns
    /// - a tuple: the finished result of a child whose declaration needed
    ///   nothing from the bridge, executed here first when it was dirty;
    /// - a list [handle, node, flags, resolved]: declared from the remembered
    ///   resolution, the bridge continues with change detection and execution;
    /// - an int handle: nothing remembered applies, the bridge resolves fully.
    /// `scoped` is whether no layout cell is active, `unbound` whether no view
    /// is bound to a cell at all, `thawed` whether no view is frozen by a
    /// resize gesture.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (parent, renderer, keyed, key, name, generation, scoped, unbound, thawed, auto_layout, input, kwargs))]
    fn declare<'py>(slf: &Bound<'py, Self>, py: Python<'py>, parent: u64, renderer: u128, keyed: bool,
                    key: Option<Bound<'py, PyAny>>, name: Option<Bound<'py, PyAny>>, generation: u64, scoped: bool,
                    unbound: bool, thawed: bool, auto_layout: bool, input: Bound<'py, PyAny>,
                    kwargs: Bound<'py, PyDict>) -> PyResult<Bound<'py, PyAny>> {
        // Phase one, under this table's borrow: identity, the remembered call
        // and the declaration itself. No view body runs here.
        let (graph, api, gpu, executor, node, flags, execute, position, extras, state) = {
            let mut guard = slf.borrow_mut();
            let this = &mut *guard;
            let (Some(graph), Some(identities)) = (&this.graph, &this.identities) else {
                return Err(pyo3::exceptions::PyRuntimeError::new_err("retained runtime is closed"));
            };
            let handle = identities.borrow_mut(py).handle(renderer, key.as_ref(), name.as_ref(), keyed)?;
            let fallback = |py: Python<'py>| Ok(handle.into_pyobject(py)?.into_any());
            let Some(slot) = this.slots.get_mut(&handle) else { return fallback(py); };
            if !scoped || slot.generation != generation
                || !slot.state.borrow(py).unchanged(slot.kwargs.bind(py), &kwargs)? {
                return fallback(py);
            }
            if slot.flow.0.is_some() || slot.flow.1.is_some() {
                let Some(api) = &this.api else { return fallback(py); };
                let cursor: (f64, f64) = if parent != 0 || auto_layout {
                    api.cursor.bind(py).call0()?.extract()?
                } else { (0., 0.) };
                if slot.flow.0.is_some_and(|x| x != cursor.0) || slot.flow.1.is_some_and(|y| y != cursor.1) {
                    return fallback(py);
                }
                if let Some(width) = slot.automatic_width {
                    let available = if parent != 0 {
                        graph.borrow_mut(py).constraints(py, parent).0 - cursor.0
                    } else {
                        api.content_region.bind(py).call0()?.get_item(0)?.extract::<f64>()?
                    };
                    if available != width { return fallback(py); }
                }
            }
            // The bridge is not needed when the same input declares the same
            // child again and nothing else is in play: no layout cell bindings,
            // no path-like input to resolve again, and a texture cache to
            // place images from.
            let same_input = slot.input.as_ref().is_some_and(|previous| previous.as_ptr() == input.as_ptr());
            let mut nodes = graph.borrow_mut(py);
            let known = nodes.known_child(slot.node, parent);
            let direct = parent != 0 && unbound && same_input && known
                && !input.is_instance_of::<PyString>() && !input.is_instance(this.path.bind(py))?
                && (this.api.is_none() || this.gpu.is_some());
            // A clean child needs no visibility test; a dirty one is culled by it.
            let clean = direct && nodes.clean_child(slot.node, parent);
            let (node, mut flags) = nodes.declare_flags(py, parent, 0, handle.into_pyobject(py)?.into_any().unbind(),
                input.clone().unbind(), slot.arguments.clone_ref(py), slot.rect, false,
                Some(slot.state.clone_ref(py)), slot.scrollable, !clean)?;
            if clean && flags & 3 != 0 && flags & 4 != 0 && nodes.is_visible(py, node) { flags |= 8; }
            drop(nodes);
            let previous_node = std::mem::replace(&mut slot.node, node);
            let previous_input = slot.input.replace(input.unbind());
            // Finish here: an unchanged child, or a dirty one this table can
            // hand straight to the executor (no pending edit, no frozen views).
            let dirty = flags & 2 != 0;
            let finish = direct && node == previous_node && flags & 1 == 0
                && (!dirty || (thawed && this.executor.is_some()));
            if !finish {
                let resolved = slot.resolved.bind(py).clone().into_any();
                drop(guard);
                drop(previous_input);
                return Ok(PyList::new(py, [handle.into_pyobject(py)?.into_any(), node.into_pyobject(py)?.into_any(),
                                           flags.into_pyobject(py)?.into_any(), resolved])?.into_any());
            }
            // Cull whole scroll tiles (flag 4: the parent scrolls). An unmeasured
            // automatic-height tile needs one capture before it can be culled.
            let execute = dirty && (flags & 4 == 0 || flags & 8 != 0 || (slot.rect.3 < 0. && !this.executor.as_ref()
                .is_some_and(|executor| executor.borrow(py).captured(node))));
            (graph.clone_ref(py), this.api.clone(), this.gpu.as_ref().map(|gpu| gpu.clone_ref(py)),
             this.executor.as_ref().map(|executor| executor.clone_ref(py)), node, flags, execute,
             slot.position, slot.extras, slot.state.clone_ref(py))
        };
        // Phase two, with no borrow held: the body may declare its own children.
        if execute {
            Executor::run_node(executor.as_ref().unwrap().bind(py), py, node, false)?;
        }
        let mut nodes = graph.borrow_mut(py);
        let (changed, value, w, h) = nodes.conclude(py, node)?;
        // The caller may have drawn a label before assigning this return. One
        // signalled refresh displays its post-edit state consistently.
        if changed { nodes.invalidate_node(parent); }
        drop(nodes);
        let (x, y) = position;
        if let Some(api) = &api {
            // Advance the parent's layout past this view; a non-scrolling
            // parent also draws the child's image into its own packet.
            api.set_cursor.bind(py).call1(((x, y),))?;
            if flags & 4 == 0 {
                let (texture, tw, th) = gpu.as_ref().unwrap().borrow(py).image(node)
                    .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("missing child texture"))?;
                let snap = |v: f64| (v + 0.5).floor();
                let (left, top, right, bottom) = (snap(x), snap(y), snap(x + w), snap(y + h));
                let draw = api.draw_list.bind(py).call0()?;
                draw.call_method1(intern!(py, "push_clip_rect"), (left, top, right, bottom, true))?;
                let image = draw.call_method1(intern!(py, "add_image"),
                    (texture, (left, top), (left + tw as f64, top + th as f64), (0, 1), (1, 0)));
                draw.call_method0(intern!(py, "pop_clip_rect"))?;
                image?;
            }
            api.dummy.bind(py).call1((w, h))?;
        }
        Ok(if extras { (changed, value, state).into_pyobject(py)?.into_any() }
           else { (changed, value).into_pyobject(py)?.into_any() })
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(graph) = &self.graph { visit.call(graph)?; }
        if let Some(identities) = &self.identities { visit.call(identities)?; }
        if let Some(gpu) = &self.gpu { visit.call(gpu)?; }
        if let Some(executor) = &self.executor { visit.call(executor)?; }
        for slot in self.slots.values() {
            visit.call(&slot.kwargs)?;
            visit.call(&slot.resolved)?;
            visit.call(&slot.arguments)?;
            visit.call(&slot.state)?;
            if let Some(input) = &slot.input { visit.call(input)?; }
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.clear();
    }
}
