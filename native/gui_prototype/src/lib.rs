//! Throwaway, dynamically shaped render runtime. No fixed DrawState schema.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyTuple};
use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
use std::time::Instant;
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
}
impl NativeState {
    fn new(shape: Rc<RefCell<Shape>>) -> Self {
        Self {
            shape,
            values: Vec::new(),
            owned: HashMap::new(),
            overrides: HashMap::new(),
            revision: 0,
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
    id: u64,
    width: f64,
    child_ns: u64,
}

#[pyclass(unsendable, module = "meltygui.core.rendering._gui_native")]
struct Runtime {
    shape: Rc<RefCell<Shape>>,
    nodes: HashMap<(u64, u64, u64, u64), (u64, Py<NativeState>, u64)>,
    keys: Py<PyDict>,
    globals: Py<PyDict>,
    imgui: Option<Py<PyAny>>,
    owner: Option<Py<PyAny>>,
    stack: Vec<Frame>,
    serial: u64,
    epoch: u64,
    scope: u64,
    width: f64,
    calls: u64,
    wrapper_ns: u64,
    body_ns: u64,
}
#[pymethods]
impl Runtime {
    #[new]
    #[pyo3(signature = (imgui=None))]
    fn new(py: Python<'_>, imgui: Option<Py<PyAny>>) -> Self {
        Self {
            shape: Rc::new(RefCell::new(Shape::default())),
            nodes: HashMap::new(),
            keys: PyDict::new(py).unbind(),
            globals: PyDict::new(py).unbind(),
            imgui,
            owner: None,
            stack: Vec::new(),
            serial: 0,
            epoch: 0,
            scope: 0,
            width: 300.0,
            calls: 0,
            wrapper_ns: 0,
            body_ns: 0,
        }
    }
    #[pyo3(signature = (owner=None, width=300.0, scope=None, **globals))]
    fn begin_frame(
        &mut self,
        py: Python<'_>,
        owner: Option<Py<PyAny>>,
        width: f64,
        scope: Option<Py<PyAny>>,
        globals: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        if !self.stack.is_empty() {
            return Err(PyRuntimeError::new_err("begin_frame during rendering"));
        }
        let scope = scope
            .or_else(|| {
                owner
                    .as_ref()
                    .and_then(|o| o.bind(py).getattr("unique").ok().map(Bound::unbind))
            })
            .unwrap_or_else(|| py.None());
        self.scope = if let Some(id) = self.keys.bind(py).get_item(&scope)? {
            id.extract()?
        } else {
            let id = self.keys.bind(py).len() as u64 + 1;
            self.keys.bind(py).set_item(scope, id)?;
            id
        };
        self.owner = owner;
        self.width = width;
        self.globals = globals
            .map(|v| v.copy())
            .transpose()?
            .unwrap_or_else(|| PyDict::new(py))
            .unbind();
        self.epoch += 1;
        self.calls = 0;
        self.wrapper_ns = 0;
        self.body_ns = 0;
        // Bound dynamic view lifetimes; retain briefly hidden collection children.
        if self.epoch % 120 == 0 {
            let cutoff = self.epoch.saturating_sub(120);
            self.nodes.retain(|_, (_, _, seen)| *seen >= cutoff);
        }
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
        self.nodes.clear();
        self.keys = PyDict::new(py).unbind();
        self.owner = None;
        self.globals = PyDict::new(py).unbind();
        Ok(())
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.keys)?;
        visit.call(&self.globals)?;
        if let Some(value) = &self.imgui {
            visit.call(value)?;
        }
        if let Some(value) = &self.owner {
            visit.call(value)?;
        }
        for (_, state, _) in self.nodes.values() {
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
    id: u64,
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
    ) -> PyResult<Self> {
        let (id, plan) = {
            let mut rt = runtime.borrow_mut(py);
            rt.serial += 1;
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
            (rt.serial, plan)
        };
        Ok(Self {
            runtime,
            func,
            defaults,
            parameters: plan,
            events,
            var_kwargs,
            id,
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
        let (state, unique, inherited_width, imgui, owner, globals) = {
            let mut rt = self.runtime.borrow_mut(py);
            let parent = rt.stack.last().map_or(0, |f| f.id);
            let key = args
                .get_item("key")?
                .or(args.get_item("name")?)
                .unwrap_or_else(|| py.None().into_bound(py));
            let key_id = if let Some(id) = rt.keys.bind(py).get_item(&key)? {
                id.extract::<u64>()?
            } else {
                let id = rt.keys.bind(py).len() as u64 + 1;
                rt.keys.bind(py).set_item(&key, id)?;
                id
            };
            let identity = (rt.scope, parent, self.id, key_id);
            if !rt.nodes.contains_key(&identity) {
                rt.serial += 1;
                let state = Py::new(py, NativeState::new(rt.shape.clone()))?;
                let serial = rt.serial;
                let epoch = rt.epoch;
                rt.nodes.insert(identity, (serial, state, epoch));
            }
            let epoch = rt.epoch;
            let (unique, state, seen) = rt.nodes.get_mut(&identity).unwrap();
            *seen = epoch;
            let (unique, state) = (*unique, state.clone_ref(py));
            let width = rt.stack.last().map_or(rt.width, |frame| frame.width);
            (
                state,
                unique,
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
            for (name, value) in args.iter() {
                ds.put(&name.extract::<String>()?, Value::from_py(&value));
            }
            ds.put("unique", Value::Int(unique as i64));
            ds.put("width", Value::Float(width));
            ds.put("height", Value::Float(height));
            ds.put("content_width", Value::Float(width));
            ds.put("content_height", Value::Float(height));
            ds.put("abs_left", Value::Float(left));
            ds.put("abs_top", Value::Float(top));
            ds.put(
                "depth",
                Value::Int(self.runtime.borrow(py).stack.len() as i64),
            );
        }
        args.set_item("input_value", &input_value)?;
        args.set_item("draw_state", &state)?;
        args.set_item("unique", unique)?;
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
                .flatten();
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
            id: unique,
            width,
            child_ns: 0,
        });
        let body_start = Instant::now();
        let result = self.func.bind(py).call((), Some(&clean)).map(Bound::unbind);
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
    m.add_class::<edges::EdgeGraph>()?;
    m.add_class::<retained::RetainedGraph>()?;
    m.add_class::<texture::TextureCache>()?;
    m.add_class::<NativeState>()?;
    m.add_class::<Runtime>()?;
    m.add_class::<Renderer>()?;
    Ok(())
}
