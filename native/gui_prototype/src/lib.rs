//! Throwaway, dynamically shaped render runtime. No fixed DrawState schema.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyTuple};
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::time::Instant;
mod identity;
mod retained;
mod texture;
mod edges;

#[derive(Default)]
struct Shape {
    slots: HashMap<String, usize>,
}
impl Shape {
    fn slot(&mut self, name: &str) -> usize {
        let next = self.slots.len();
        *self.slots.entry(name.to_owned()).or_insert(next)
    }
}

enum Value {
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Object(Py<PyAny>),
}
impl Value {
    fn from_py(value: &Bound<'_, PyAny>) -> Self {
        if value.is_none() {
            Self::None
        } else if value.is_exact_instance_of::<PyBool>() {
            Self::Bool(value.extract().unwrap())
        } else if value.is_exact_instance_of::<PyInt>() {
            value
                .extract()
                .map(Self::Int)
                .unwrap_or_else(|_| Self::Object(value.clone().unbind()))
        } else if value.is_exact_instance_of::<PyFloat>() {
            Self::Float(value.extract().unwrap())
        } else {
            Self::Object(value.clone().unbind())
        }
    }
    fn to_py(&self, py: Python<'_>) -> Py<PyAny> {
        match self {
            Self::None => py.None(),
            Self::Bool(v) => v.into_pyobject(py).unwrap().to_owned().into_any().unbind(),
            Self::Int(v) => v.into_pyobject(py).unwrap().into_any().unbind(),
            Self::Float(v) => v.into_pyobject(py).unwrap().into_any().unbind(),
            Self::Object(v) => v.clone_ref(py),
        }
    }
    fn number(&self) -> Option<f64> {
        match self {
            Self::Int(v) => Some(*v as f64),
            Self::Float(v) => Some(*v),
            _ => None,
        }
    }
}

#[pyclass(unsendable, module = "meltygui.core.rendering._gui_native")]
struct NativeState {
    shape: Rc<RefCell<Shape>>,
    values: Vec<Option<Value>>,
    owned: HashMap<usize, Py<PyAny>>,
    overrides: HashMap<usize, Value>,
    revision: u64,
    excluded: HashSet<String>,
    renderer: u128,
}
const TRANSIENT_STATE: &[&str] = &[
    "input_value", "draw_state", "cache", "view_events", "font_manager", "style_manager",
    "width", "height", "content_width", "content_height", "abs_left", "abs_top",
    "measured_width", "depth", "unique_events",
];
impl NativeState {
    fn new(shape: Rc<RefCell<Shape>>) -> Self {
        let mut state = Self {
            shape,
            values: Vec::new(),
            owned: HashMap::new(),
            overrides: HashMap::new(),
            revision: 0,
            excluded: TRANSIENT_STATE.iter().map(|v| v.to_string()).collect(),
            renderer: 0,
        };
        state.put("unique", Value::Int(0));
        state.put("dlt_count", Value::Int(0));
        state
    }
    fn rebind(&mut self, shape: Rc<RefCell<Shape>>) {
        if Rc::ptr_eq(&self.shape, &shape) { return; }
        let mut state = Self::new(shape);
        for (name, slot) in self.shape.borrow().slots.iter() {
            let target = state.shape.borrow_mut().slot(name);
            if let Some(value) = self.values.get_mut(*slot).and_then(Option::take) { state.put_slot(target, value); }
            if let Some(value) = self.owned.remove(slot) { state.owned.insert(target, value); }
            if let Some(value) = self.overrides.remove(slot) { state.overrides.insert(target, value); }
        }
        state.excluded = std::mem::take(&mut self.excluded);
        *self = state;
    }
    fn release_transients(&mut self) {
        for (name, slot) in self.shape.borrow().slots.iter() {
            if name.starts_with('_') || self.excluded.contains(name) {
                if let Some(value) = self.values.get_mut(*slot) { *value = None; }
                self.owned.remove(slot);
                self.overrides.remove(slot);
            }
        }
    }
    fn put_slot(&mut self, slot: usize, value: Value) {
        if self.values.len() <= slot {
            self.values.resize_with(slot + 1, || None);
        }
        self.values[slot] = Some(value);
        self.revision += 1;
    }
    fn put(&mut self, name: &str, value: Value) {
        let slot = self.shape.borrow_mut().slot(name);
        self.put_slot(slot, value);
    }
    fn at(&self, slot: usize) -> Option<&Value> {
        self.values.get(slot).and_then(Option::as_ref)
    }
    fn lookup(&self, name: &str) -> Option<&Value> {
        let slot = self.shape.borrow().slots.get(name).copied()?;
        self.at(slot)
    }
    fn number(&self, name: &str, default: f64) -> f64 {
        self.lookup(name).and_then(Value::number).unwrap_or(default)
    }
}
#[pymethods]
impl NativeState {
    #[new]
    fn py_new() -> Self { Self::new(Rc::new(RefCell::new(Shape::default()))) }

    #[pyo3(signature = (rect, excluded=Vec::new()))]
    fn save_window_geometry(&mut self, py: Python<'_>, rect: (f64, f64, f64, f64), excluded: Vec<String>) -> PyResult<()> {
        // The retained graph owns window placement; capture it only at save/close.
        self.put("window_pos", Value::Object((rect.0, rect.1).into_pyobject(py)?.into_any().unbind()));
        self.put("width", Value::Float(rect.2));
        self.put("height", Value::Float(rect.3));
        for name in ["window_pos", "width", "height"] {
            if let Some(slot) = self.shape.borrow().slots.get(name) { self.overrides.remove(slot); }
            if excluded.iter().any(|field| field == name) { self.excluded.insert(name.to_string()); }
            else { self.excluded.remove(name); }
        }
        Ok(())
    }

    #[pyo3(signature = (excluded=Vec::new()))]
    fn save_state(&self, py: Python<'_>, excluded: Vec<String>) -> PyResult<Py<PyAny>> {
        let values = PyDict::new(py);
        let owned = PyDict::new(py);
        let overrides = PyDict::new(py);
        for (name, slot) in self.shape.borrow().slots.iter() {
            if name.starts_with('_') || self.excluded.contains(name) || excluded.contains(name) { continue; }
            if let Some(value) = self.at(*slot) { values.set_item(name, value.to_py(py))?; }
            if let Some(value) = self.owned.get(slot) { owned.set_item(name, value)?; }
            if let Some(value) = self.overrides.get(slot) { overrides.set_item(name, value.to_py(py))?; }
        }
        values.set_item("dlt_count", self.number("dlt_count", 0.) as i64 - 1)?;
        Ok((values, owned, overrides).into_pyobject(py)?.into_any().unbind())
    }
    fn __setstate__(&mut self, state: &Bound<'_, PyTuple>) -> PyResult<()> {
        for (index, item) in state.iter().enumerate() {
            for (key, value) in item.downcast::<PyDict>()?.iter() {
                let name = key.extract::<String>()?;
                let slot = self.shape.borrow_mut().slot(&name);
                match index {
                    0 => self.put_slot(slot, Value::from_py(&value)),
                    1 => { self.owned.insert(slot, value.unbind()); },
                    2 => { self.overrides.insert(slot, Value::from_py(&value)); },
                    _ => return Err(PyTypeError::new_err("invalid native draw state")),
                }
            }
        }
        Ok(())
    }
    fn __getattr__(&self, py: Python<'_>, name: &str) -> PyResult<Py<PyAny>> {
        self.lookup(name)
            .map(|v| v.to_py(py))
            .ok_or_else(|| PyAttributeError::new_err(name.to_owned()))
    }
    fn __setattr__(&mut self, name: &str, value: &Bound<'_, PyAny>) {
        let slot = self.shape.borrow_mut().slot(name);
        self.put_slot(slot, Value::from_py(value));
        self.overrides.insert(slot, Value::from_py(value));
    }
    fn __delattr__(&mut self, name: &str) -> PyResult<()> {
        let slot = self.shape.borrow().slots.get(name).copied();
        if let Some(slot) = slot {
            self.overrides.remove(&slot);
            if let Some(value) = self.values.get_mut(slot) {
                if value.take().is_some() {
                    self.revision += 1;
                    return Ok(());
                }
            }
        }
        Err(PyAttributeError::new_err(name.to_owned()))
    }
    #[pyo3(signature = (name, default=None))]
    fn get(&self, py: Python<'_>, name: &str, default: Option<Py<PyAny>>) -> Py<PyAny> {
        self.lookup(name)
            .map(|v| v.to_py(py))
            .unwrap_or_else(|| default.unwrap_or_else(|| py.None()))
    }
    fn as_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let result = PyDict::new(py);
        for (name, slot) in &self.shape.borrow().slots {
            if let Some(value) = self.at(*slot) {
                result.set_item(name, value.to_py(py))?;
            }
        }
        Ok(result.unbind())
    }
    #[getter]
    fn revision(&self) -> u64 {
        self.revision
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for value in self.values.iter().flatten() {
            if let Value::Object(value) = value {
                visit.call(value)?;
            }
        }
        for value in self.owned.values() {
            visit.call(value)?;
        }
        for value in self.overrides.values() {
            if let Value::Object(value) = value {
                visit.call(value)?;
            }
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.values.clear();
        self.owned.clear();
        self.overrides.clear();
    }
}

struct Frame {
    width: f64,
    child_ns: u64,
}

#[pyclass(unsendable, module = "meltygui.core.rendering._gui_native")]
struct Runtime {
    shape: Rc<RefCell<Shape>>,
    nodes: HashMap<u128, (Py<NativeState>, Py<PyAny>)>,
    identities: Py<identity::Identities>,
    globals: Py<PyDict>,
    imgui: Option<Py<PyAny>>,
    owner: Option<Py<PyAny>>,
    stack: Vec<Frame>,
    width: f64,
    calls: u64,
    wrapper_ns: u64,
    body_ns: u64,
}
#[pymethods]
impl Runtime {
    #[getter]
    fn identities(&self, py: Python<'_>) -> Py<identity::Identities> {
        self.identities.clone_ref(py)
    }
    #[new]
    #[pyo3(signature = (imgui=None, identities=None))]
    fn new(py: Python<'_>, imgui: Option<Py<PyAny>>, identities: Option<Py<identity::Identities>>) -> PyResult<Self> {
        Ok(Self {
            shape: Rc::new(RefCell::new(Shape::default())),
            nodes: HashMap::new(),
            identities: match identities { Some(ids) => ids, None => Py::new(py, identity::Identities::new(py, None))? },
            globals: PyDict::new(py).unbind(),
            imgui,
            owner: None,
            stack: Vec::new(),
            width: 300.0,
            calls: 0,
            wrapper_ns: 0,
            body_ns: 0,
        })
    }
    #[pyo3(signature = (owner=None, width=300.0, scope=None, identity_frame=true, **globals))]
    fn begin_frame(
        &mut self,
        py: Python<'_>,
        owner: Option<Py<PyAny>>,
        width: f64,
        scope: Option<Py<PyAny>>,
        identity_frame: bool,
        globals: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        if !self.stack.is_empty() {
            return Err(PyRuntimeError::new_err("begin_frame during rendering"));
        }
        if let Some(scope) = scope {
            self.identities.borrow_mut(py).set_scope(scope.bind(py))?;
        }
        if identity_frame { self.identities.borrow_mut(py).begin_frame(); }
        self.owner = owner;
        self.width = width;
        self.globals = globals
            .map(|v| v.copy())
            .transpose()?
            .unwrap_or_else(|| PyDict::new(py))
            .unbind();
        self.calls = 0;
        self.wrapper_ns = 0;
        self.body_ns = 0;
        Ok(())
    }
    fn end_frame(&mut self, py: Python<'_>) -> PyResult<()> {
        if !self.stack.is_empty() {
            return Err(PyRuntimeError::new_err("unbalanced gui calls"));
        }
        self.owner = None;
        self.globals = PyDict::new(py).unbind();
        Ok(())
    }
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let result = PyDict::new(py);
        result.set_item("calls", self.calls)?;
        result.set_item("wrapper_us", self.wrapper_ns as f64 / 1000.0)?;
        result.set_item("body_us", self.body_ns as f64 / 1000.0)?;
        result.set_item("nodes", self.nodes.len())?;
        result.set_item("fields", self.shape.borrow().slots.len())?;
        result.set_item("stack_depth", self.stack.len())?;
        Ok(result.unbind())
    }
    fn clear(&mut self, py: Python<'_>) -> PyResult<()> {
        if !self.stack.is_empty() {
            return Err(PyRuntimeError::new_err("clear during rendering"));
        }
        for (unique, (state, _)) in &self.nodes {
            state.borrow_mut(py).release_transients();
            let ids = self.identities.borrow(py);
            if !ids.persistent { ids.registry.bind(py).del_item(unique).ok(); }
        }
        self.nodes.clear();
        self.owner = None;
        self.globals = PyDict::new(py).unbind();
        Ok(())
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.identities)?;
        visit.call(&self.globals)?;
        if let Some(value) = &self.imgui {
            visit.call(value)?;
        }
        if let Some(value) = &self.owner {
            visit.call(value)?;
        }
        for (state, _) in self.nodes.values() {
            visit.call(state)?;
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.nodes.clear();
        self.owner = None;
    }
}

struct Parameter {
    name: String,
    slot: usize,
    default: Option<Py<PyAny>>,
    factory: Option<Py<PyAny>>,
}

#[pyclass(unsendable, module = "meltygui.core.rendering._gui_native")]
struct Renderer {
    runtime: Py<Runtime>,
    func: Py<PyAny>,
    defaults: Py<PyDict>,
    parameters: Vec<Parameter>,
    events: Vec<String>,
    var_kwargs: bool,
    excluded: Vec<String>,
    id: u128,
}

fn kw_number(kwargs: &Bound<'_, PyDict>, name: &str, default: f64) -> f64 {
    kwargs
        .get_item(name)
        .ok()
        .flatten()
        .and_then(|v| v.extract().ok())
        .unwrap_or(default)
}

#[pymethods]
impl Renderer {
    fn reconfigure(&mut self, py: Python<'_>, replacement: &Renderer) {
        self.func = replacement.func.clone_ref(py);
        self.defaults = replacement.defaults.clone_ref(py);
        self.events = replacement.events.clone();
        self.var_kwargs = replacement.var_kwargs;
        self.excluded = replacement.excluded.clone();
        for (state, _) in self.runtime.borrow(py).nodes.values() {
            let mut state = state.borrow_mut(py);
            if state.renderer == self.id {
                state.excluded = TRANSIENT_STATE.iter().map(|v| v.to_string()).chain(self.excluded.iter().cloned()).collect();
            }
        }
        self.parameters = replacement
            .parameters
            .iter()
            .map(|p| Parameter {
                name: p.name.clone(),
                slot: p.slot,
                default: p.default.as_ref().map(|v| v.clone_ref(py)),
                factory: p.factory.as_ref().map(|v| v.clone_ref(py)),
            })
            .collect();
    }
    #[new]
    fn new(
        py: Python<'_>,
        runtime: Py<Runtime>,
        func: Py<PyAny>,
        defaults: Py<PyDict>,
        parameters: Vec<(String, bool, Py<PyAny>, Option<Py<PyAny>>)>,
        events: Vec<String>,
        var_kwargs: bool,
        identity: u128,
        excluded: Vec<String>,
    ) -> PyResult<Self> {
        let plan = {
            let rt = runtime.borrow(py);
            let plan = parameters
                .into_iter()
                .map(|(name, has_default, default, factory)| {
                    let slot = rt.shape.borrow_mut().slot(&name);
                    Parameter {
                        name,
                        slot,
                        default: has_default.then_some(default),
                        factory,
                    }
                })
                .collect();
            plan
        };
        Ok(Self {
            runtime,
            func,
            defaults,
            parameters: plan,
            events,
            var_kwargs,
            excluded,
            id: identity,
        })
    }
    #[pyo3(signature = (input_value=None, **kwargs))]
    fn __call__(
        &self,
        py: Python<'_>,
        input_value: Option<Py<PyAny>>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        let start = Instant::now();
        let input_value = input_value.unwrap_or_else(|| py.None());
        let args = self.defaults.bind(py).copy()?;
        if let Some(kwargs) = kwargs {
            args.update(kwargs.as_mapping())?;
        }
        // Cached calls carry defaults for layout; only caller keys outrank state.
        let explicit_keys = args.get_item("_gui_explicit")?;
        if explicit_keys.is_some() { args.del_item("_gui_explicit")?; }
        let (state, unique, unique_value, inherited_width, imgui, owner, globals) = {
            let mut rt = self.runtime.borrow_mut(py);
            let unique = if let Some(value) = args.get_item("_gui_unique")? {
                let unique = value.extract::<u128>()?;
                args.del_item("_gui_unique")?;
                unique
            } else {
                rt.identities.borrow_mut(py).resolve(self.id, args.get_item("key")?.as_ref(), args.get_item("name")?.as_ref(), args.contains("key")?)?
            };
            if !rt.nodes.contains_key(&unique) {
                let identities = rt.identities.clone_ref(py);
                let mut ids = identities.borrow_mut(py);
                let saved = ids.registry.bind(py).get_item(unique)?;
                let state = match saved.and_then(|s| s.extract::<Py<NativeState>>().ok()) {
                    Some(state) => { state.borrow_mut(py).rebind(rt.shape.clone()); state },
                    None => Py::new(py, NativeState::new(rt.shape.clone()))?,
                };
                {
                    let mut ds = state.borrow_mut(py);
                    ds.renderer = self.id;
                    ds.excluded = TRANSIENT_STATE.iter().map(|v| v.to_string()).chain(self.excluded.iter().cloned()).collect();
                }
                ids.registry.bind(py).set_item(unique, &state)?;
                ids.states.insert(unique);
                rt.nodes.insert(unique, (state, unique.into_pyobject(py)?.into_any().unbind()));
            }
            let (state, unique_value) = rt.nodes.get(&unique).unwrap();
            let state = state.clone_ref(py);
            let unique_value = unique_value.clone_ref(py);
            let width = rt.stack.last().map_or(rt.width, |frame| frame.width);
            (
                state,
                unique,
                unique_value,
                width,
                rt.imgui.as_ref().map(|v| v.clone_ref(py)),
                rt.owner.as_ref().map(|v| v.clone_ref(py)),
                rt.globals.clone_ref(py),
            )
        };
        for (key, value) in globals.bind(py).iter() {
            if !args.contains(&key)? {
                args.set_item(key, value)?;
            }
        }
        let width = kw_number(&args, "width", inherited_width).max(1.0);
        let height = kw_number(&args, "height", state.borrow(py).number("height", 24.0)).max(1.0);
        let (left, top) = if let Some(imgui) = &imgui {
            imgui
                .bind(py)
                .call_method0("get_cursor_screen_pos")?
                .extract::<(f64, f64)>()?
        } else {
            (0.0, 0.0)
        };
        {
            let mut ds = state.borrow_mut(py);
            ds.put("unique", Value::Object(unique_value.clone_ref(py)));
            ds.put("dlt_count", Value::Int(1));
            ds.put("width", Value::Float(width));
            ds.put("height", Value::Float(height));
            ds.put("content_width", Value::Float(width));
            ds.put("content_height", Value::Float(height));
            ds.put("abs_left", Value::Float(left));
            ds.put("abs_top", Value::Float(top));
        }
        args.set_item("input_value", &input_value)?;
        args.set_item("draw_state", &state)?;
        args.set_item("unique", &unique_value)?;
        args.set_item("depth", self.runtime.borrow(py).stack.len())?;
        if !self.events.is_empty() {
            if let Some(owner) = owner {
                let event_args = PyDict::new(py);
                event_args.set_item("view_id", format!("gui-{unique}"))?;
                event_args.set_item("rect", (left, top, left + width, top + height))?;
                event_args.set_item("priority_delta", 0)?;
                let events =
                    owner
                        .bind(py)
                        .call_method("on_action", (&self.events,), Some(&event_args))?;
                if let Ok(events) = events.downcast::<PyDict>() {
                    for (name, value) in events.iter() {
                        if kwargs.map_or(true, |kw| !kw.contains(&name).unwrap_or(false)) {
                            args.set_item(name, value)?;
                        }
                    }
                }
            }
        }
        for name in &self.events {
            if !args.contains(name)? {
                args.set_item(name, py.None())?;
            }
        }
        let clean = if self.var_kwargs {
            args.copy()?
        } else {
            PyDict::new(py)
        };
        for parameter in &self.parameters {
            let authored = state
                .borrow(py)
                .overrides
                .get(&parameter.slot)
                .map(|v| v.to_py(py));
            let explicit = kwargs
                .map(|kw| kw.get_item(&parameter.name))
                .transpose()?
                .flatten()
                .filter(|_| explicit_keys.as_ref().map_or(true, |keys| keys.contains(&parameter.name).unwrap_or(false)));
            let value = if let Some(value) = explicit {
                value.unbind()
            } else if let Some(value) = authored {
                value
            } else if let Some(value) = args.get_item(&parameter.name)? {
                value.unbind()
            } else if let Some(factory) = &parameter.factory {
                let previous = state
                    .borrow(py)
                    .owned
                    .get(&parameter.slot)
                    .map(|v| v.clone_ref(py));
                if let Some(value) = previous {
                    value
                } else {
                    // Never hold a Rust borrow while invoking arbitrary Python.
                    let value = factory.bind(py).call0()?.unbind();
                    state
                        .borrow_mut(py)
                        .owned
                        .insert(parameter.slot, value.clone_ref(py));
                    value
                }
            } else if let Some(value) = &parameter.default {
                value.clone_ref(py)
            } else {
                return Err(PyTypeError::new_err(format!(
                    "missing gui argument: {}",
                    parameter.name
                )));
            };
            clean.set_item(&parameter.name, &value)?;
            // Framework-injected self references are transient, not persisted in state.
            if parameter.name != "draw_state" && parameter.name != "input_value" {
                state
                    .borrow_mut(py)
                    .put_slot(parameter.slot, Value::from_py(value.bind(py)));
            }
        }
        if let Some(imgui) = &imgui {
            imgui
                .bind(py)
                .call_method1("push_id", (format!("gui-{unique}"),))?;
            if let Err(error) = imgui.bind(py).call_method0("begin_group") {
                imgui.bind(py).call_method0("pop_id")?;
                return Err(error);
            }
        }
        self.runtime.borrow_mut(py).stack.push(Frame {
            width,
            child_ns: 0,
        });
        let identities = self.runtime.borrow(py).identities.clone_ref(py);
        identities.borrow_mut(py).enter(unique);
        let body_start = Instant::now();
        let result = self.func.bind(py).call((), Some(&clean)).map(Bound::unbind);
        identities.borrow_mut(py).leave(result.is_ok());
        let body_ns = body_start.elapsed().as_nanos() as u64;
        let child_ns = self.runtime.borrow_mut(py).stack.pop().unwrap().child_ns;
        let cleanup = (|| -> PyResult<()> {
            if let Some(imgui) = &imgui {
                let imgui = imgui.bind(py);
                let end = imgui.call_method0("end_group");
                let pop = imgui.call_method0("pop_id");
                end?;
                pop?;
                let (measured_w, measured_h) = imgui
                    .call_method0("get_item_rect_size")?
                    .extract::<(f64, f64)>()?;
                let mut ds = state.borrow_mut(py);
                ds.put("content_height", Value::Float(measured_h));
                ds.put(
                    "height",
                    Value::Float(kw_number(&args, "height", measured_h)),
                );
                ds.put("measured_width", Value::Float(measured_w));
            }
            Ok(())
        })();
        let elapsed = start.elapsed().as_nanos() as u64;
        {
            let mut rt = self.runtime.borrow_mut(py);
            rt.calls += 1;
            rt.wrapper_ns += elapsed.saturating_sub(body_ns);
            rt.body_ns += body_ns.saturating_sub(child_ns);
            if let Some(parent) = rt.stack.last_mut() {
                parent.child_ns += elapsed;
            }
        }
        let result = result?;
        cleanup?;
        // Drawing-only functions need neither an input parameter nor a return.
        // Preserve the caller's value so they compose with editable views.
        let result = if result.is_none(py) {
            (false, input_value).into_pyobject(py)?.into_any().unbind()
        } else {
            result
        };
        if args
            .get_item("return_extras")?
            .is_some_and(|v| v.is_truthy().unwrap_or(false))
        {
            let result = result.bind(py).downcast::<PyTuple>()?;
            return Ok((result.get_item(0)?, result.get_item(1)?, state)
                .into_pyobject(py)?
                .into_any()
                .unbind());
        }
        Ok(result)
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.runtime)?;
        visit.call(&self.func)?;
        visit.call(&self.defaults)?;
        for param in &self.parameters {
            if let Some(value) = &param.default {
                visit.call(value)?;
            }
            if let Some(value) = &param.factory {
                visit.call(value)?;
            }
        }
        Ok(())
    }
}

#[pymodule]
fn _gui_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<identity::Identities>()?;
    m.add_class::<edges::EdgeGraph>()?;
    m.add_class::<retained::RetainedGraph>()?;
    m.add_class::<texture::TextureCache>()?;
    m.add_class::<NativeState>()?;
    m.add_class::<Runtime>()?;
    m.add_class::<Renderer>()?;
    Ok(())
}
