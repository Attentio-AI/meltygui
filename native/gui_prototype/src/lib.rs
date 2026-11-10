//! Throwaway, dynamically shaped render runtime. No fixed DrawState schema.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyTypeError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyTuple};
use std::cell::RefCell;
use crate::hash::{Map as HashMap, Set as HashSet};
use std::rc::{Rc, Weak};
use std::time::Instant;
mod hash;
mod declare;
mod executor;
mod geometry;
mod identity;
mod retained;
mod texture;
mod edges;

struct Shape {
    slots: HashMap<String, usize>,
    effects: Vec<u8>,
}
// Framework fields occupy fixed leading slots in every shape, so the hot
// render path writes them by index instead of hashing their names.
const CORE_FIELDS: [&str; 11] = [
    "unique", "dlt_count", "left", "top", "width", "height", "scroll_offset",
    "content_offset", "content_width", "content_height", "measured_width",
];
const SLOT_UNIQUE: usize = 0;
const SLOT_DLT_COUNT: usize = 1;
const SLOT_LEFT: usize = 2;
const SLOT_TOP: usize = 3;
const SLOT_WIDTH: usize = 4;
const SLOT_HEIGHT: usize = 5;
const SLOT_SCROLL_OFFSET: usize = 6;
const SLOT_CONTENT_OFFSET: usize = 7;
const SLOT_CONTENT_WIDTH: usize = 8;
const SLOT_CONTENT_HEIGHT: usize = 9;
const SLOT_MEASURED_WIDTH: usize = 10;
impl Default for Shape {
    fn default() -> Self {
        let mut shape = Self { slots: HashMap::default(), effects: Vec::new() };
        for name in CORE_FIELDS { shape.slot(name); }
        shape
    }
}
impl Shape {
    fn slot(&mut self, name: &str) -> usize {
        if let Some(slot) = self.slots.get(name) { return *slot; }
        let slot = self.slots.len();
        self.slots.insert(name.to_owned(), slot);
        self.effects.push(geometry::effect(name));
        slot
    }
}

enum Value {
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Point(f64, f64),
    Object(Py<PyAny>),
}
impl Value {
    fn same(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::None, Self::None) => true,
            (Self::Bool(a), Self::Bool(b)) => a == b,
            (Self::Int(a), Self::Int(b)) => a == b,
            (Self::Float(a), Self::Float(b)) => a == b,
            (Self::Point(x,y), Self::Point(a,b)) => x==a && y==b,
            (Self::Object(a), Self::Object(b)) => a.as_ptr()==b.as_ptr(),
            _ => false,
        }
    }
    fn field(name: &str, value: &Bound<'_, PyAny>) -> PyResult<Self> {
        if matches!(name, "left" | "top") && !value.is_none() {
            let number=value.extract::<f64>()?;
            if !number.is_finite() {return Err(PyTypeError::new_err("position must be finite"));}
            return Ok(Self::Float(number));
        }
        if matches!(name, "width" | "height") && !value.is_none() {
            let number=value.extract::<f64>()?;
            if !number.is_finite() || number<0. {return Err(PyTypeError::new_err("size must be finite and nonnegative"));}
            return Ok(Self::Float(number));
        }
        if matches!(name, "scroll_offset" | "content_offset") && !value.is_none() {
            let (x,y)=value.extract::<(f64,f64)>()?;
            if !x.is_finite() || !y.is_finite() {return Err(PyTypeError::new_err("geometry must be finite"));}
            return Ok(Self::Point(x,y));
        }
        Ok(Self::from_py(value))
    }
    fn point(&self) -> Option<(f64,f64)> { if let Self::Point(x,y)=self {Some((*x,*y))} else {None} }
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
            Self::Point(x,y) => (*x,*y).into_pyobject(py).unwrap().into_any().unbind(),
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
pub(crate) struct NativeState {
    shape: Rc<RefCell<Shape>>,
    values: Vec<Option<Value>>,
    owned: HashMap<usize, Py<PyAny>>,
    overrides: HashMap<usize, Value>,
    revision: u64,
    // Placement, size and scroll values as the last retained declaration
    // resolved them. While the current values still match and the call's
    // inputs are unchanged, reconfiguring would write the same values again.
    resolved: Option<[Option<f64>; 8]>,
    excluded: HashSet<String>,
    renderer: u128,
    observer: Option<(u64, Weak<RefCell<HashMap<u64,u8>>>)>,
    rendering: bool,
}
const TRANSIENT_STATE: &[&str] = &[
    "input_value", "draw_state", "cache", "view_events", "font_manager", "style_manager",
    "content_width", "content_height", "abs_left", "abs_top",
    "measured_width", "depth", "unique_events",
];
impl NativeState {
    fn new(shape: Rc<RefCell<Shape>>) -> Self {
        let mut state = Self {
            shape,
            values: Vec::new(),
            owned: HashMap::default(),
            overrides: HashMap::default(),
            revision: 0,
            resolved: None,
            excluded: TRANSIENT_STATE.iter().map(|v| v.to_string()).collect(),
            renderer: 0,
            observer: None,
            rendering: false,
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
            if matches!(name.as_str(), "cache" | "view_events" | "font_manager" | "style_manager") {
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
        if self.at(slot).is_some_and(|old| old.same(&value)) { return; }
        self.values[slot] = Some(value);
        self.revision += 1;
        if let Some((node, queue)) = &self.observer {
            let effect = self.shape.borrow().effects[slot];
            if effect != 0 && !(self.rendering && effect == geometry::CONTENT) {
                if let Some(queue) = queue.upgrade() { *queue.borrow_mut().entry(*node).or_default() |= effect; }
            }
        }
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
    // Left, top, width, height, then the scroll and content offsets.
    fn geometry_values(&self) -> [Option<f64>; 8] {
        let number = |slot: usize| self.at(slot).and_then(Value::number);
        let point = |slot: usize| self.at(slot).and_then(Value::point);
        let (scroll, content) = (point(SLOT_SCROLL_OFFSET), point(SLOT_CONTENT_OFFSET));
        [number(SLOT_LEFT), number(SLOT_TOP), number(SLOT_WIDTH), number(SLOT_HEIGHT),
         scroll.map(|p| p.0), scroll.map(|p| p.1), content.map(|p| p.0), content.map(|p| p.1)]
    }
}
#[pymethods]
impl NativeState {
    #[new]
    fn py_new() -> Self { Self::new(Rc::new(RefCell::new(Shape::default()))) }

    fn configure(&mut self, defaults: &Bound<'_, PyDict>, caller: &Bound<'_, PyDict>, initial: &Bound<'_, PyDict>) -> PyResult<()> {
        for (name, value) in initial.iter() {
            let name=name.extract::<String>()?;
            let slot=self.shape.borrow_mut().slot(&name);
            if self.at(slot).is_none() {
                self.overrides.insert(slot, Value::field(&name,&value)?);
                self.put_slot(slot, Value::field(&name,&value)?);
            }
        }
        for (name, value) in defaults.iter() {
            let name=name.extract::<String>()?;
            let slot=self.shape.borrow_mut().slot(&name);
            let value=if let Some(explicit)=caller.get_item(&name)? {Value::field(&name,&explicit)?}
                else if let Some(authored)=self.overrides.get(&slot) {Value::field(&name,authored.to_py(defaults.py()).bind(defaults.py()))?}
                else {Value::field(&name,&value)?};
            self.put_slot(slot,value);
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
                // Translate saved identifiers only; live state has no tuple-position alias.
                if name == "window_pos" {
                    let (left,top)=value.extract::<(f64,f64)>()?;
                    for (axis,number) in [("left",left),("top",top)] {
                        if item.downcast::<PyDict>()?.contains(axis)? {continue;}
                        let slot=self.shape.borrow_mut().slot(axis);
                        match index {
                            0 => {
                                self.put_slot(slot,Value::Float(number));
                                self.overrides.insert(slot,Value::Float(number));
                            },
                            2 => {self.overrides.insert(slot,Value::Float(number));},
                            _ => {},
                        }
                    }
                    continue;
                }
                let slot = self.shape.borrow_mut().slot(&name);
                match index {
                    0 => self.put_slot(slot, Value::field(&name, &value)?),
                    1 => { self.owned.insert(slot, value.unbind()); },
                    2 => { self.overrides.insert(slot, Value::field(&name, &value)?); },
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
    fn __setattr__(&mut self, name: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        if name=="window_pos" {return Err(PyAttributeError::new_err("use left and top for relative position"));}
        let slot = self.shape.borrow_mut().slot(name);
        self.put_slot(slot, Value::field(name,value)?);
        self.overrides.insert(slot, Value::field(name,value)?);
        Ok(())
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
    /// Record the geometry a retained declaration just resolved.
    pub(crate) fn remember_geometry(&mut self) {
        self.resolved = Some(self.geometry_values());
    }
    /// Whether a declaration memoized with keyword arguments `previous` still
    /// applies to a call with `current`: placement, size and scroll still hold
    /// the remembered values and the arguments are the same. Values rewritten
    /// in between with the same result do not matter.
    pub(crate) fn unchanged(&self, previous: &Bound<'_, PyDict>, current: &Bound<'_, PyDict>) -> PyResult<bool> {
        Ok(self.resolved == Some(self.geometry_values()) && same_arguments(previous, current)?)
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

// ImGui entry points used around every body, resolved once per runtime so a
// view execution does not repeat the module attribute lookups.
struct Api {
    cursor: Py<PyAny>,
    set_cursor: Py<PyAny>,
    push_id: Py<PyAny>,
    pop_id: Py<PyAny>,
    begin_group: Py<PyAny>,
    end_group: Py<PyAny>,
    item_rect_size: Py<PyAny>,
}

#[pyclass(unsendable, module = "meltygui.core.rendering._gui_native")]
struct Runtime {
    shape: Rc<RefCell<Shape>>,
    // State, the unique ID as a Python int, and its ImGui ID scope label.
    nodes: HashMap<u128, (Py<NativeState>, Py<PyAny>, Py<PyString>)>,
    identities: Py<identity::Identities>,
    globals: Py<PyDict>,
    imgui: Option<Py<PyAny>>,
    api: Option<Rc<Api>>,
    owner: Option<Py<PyAny>>,
    stack: Vec<Frame>,
    width: f64,
    calls: u64,
    wrapper_ns: u64,
    body_ns: u64,
}
impl Runtime {
    fn ensure_state(&mut self, py: Python<'_>, unique: u128) -> PyResult<()> {
            if !self.nodes.contains_key(&unique) {
                let identities = self.identities.clone_ref(py);
                let mut ids = identities.borrow_mut(py);
                let saved = ids.registry.bind(py).get_item(unique)?;
                let state = match saved.and_then(|s| s.extract::<Py<NativeState>>().ok()) {
                    Some(state) => { state.borrow_mut(py).rebind(self.shape.clone()); state },
                    None => Py::new(py, NativeState::new(self.shape.clone()))?,
                };
                ids.registry.bind(py).set_item(unique, &state)?;
                ids.states.insert(unique);
                let unique_value=unique.into_pyobject(py)?.into_any().unbind();
                state.borrow_mut(py).put("unique",Value::Object(unique_value.clone_ref(py)));
                let label=PyString::new(py,&format!("gui-{unique}")).unbind();
                self.nodes.insert(unique, (state, unique_value, label));
            }
        Ok(())
    }
    fn api(&mut self, py: Python<'_>) -> PyResult<Option<Rc<Api>>> {
        if self.api.is_none() {
            if let Some(imgui) = &self.imgui {
                let module = imgui.bind(py);
                self.api = Some(Rc::new(Api {
                    cursor: module.getattr(intern!(py, "get_cursor_screen_pos"))?.unbind(),
                    set_cursor: module.getattr(intern!(py, "set_cursor_screen_pos"))?.unbind(),
                    push_id: module.getattr(intern!(py, "push_id"))?.unbind(),
                    pop_id: module.getattr(intern!(py, "pop_id"))?.unbind(),
                    begin_group: module.getattr(intern!(py, "begin_group"))?.unbind(),
                    end_group: module.getattr(intern!(py, "end_group"))?.unbind(),
                    item_rect_size: module.getattr(intern!(py, "get_item_rect_size"))?.unbind(),
                }));
            }
        }
        Ok(self.api.clone())
    }
}
#[pymethods]
impl Runtime {
    fn state(&mut self, py: Python<'_>, unique: u128) -> PyResult<Py<NativeState>> {
        self.ensure_state(py,unique)?;
        Ok(self.nodes[&unique].0.clone_ref(py))
    }
    #[getter]
    fn identities(&self, py: Python<'_>) -> Py<identity::Identities> {
        self.identities.clone_ref(py)
    }
    #[new]
    #[pyo3(signature = (imgui=None, identities=None))]
    fn new(py: Python<'_>, imgui: Option<Py<PyAny>>, identities: Option<Py<identity::Identities>>) -> PyResult<Self> {
        Ok(Self {
            shape: Rc::new(RefCell::new(Shape::default())),
            nodes: HashMap::default(),
            identities: match identities { Some(ids) => ids, None => Py::new(py, identity::Identities::new(py, None))? },
            globals: PyDict::new(py).unbind(),
            imgui,
            api: None,
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
        for (unique, (state, _, _)) in &self.nodes {
            state.borrow_mut(py).release_transients();
            let ids=self.identities.borrow(py);
            if !ids.persistent {ids.registry.bind(py).del_item(unique).ok();}
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
        for (state, _, _) in self.nodes.values() {
            visit.call(state)?;
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.nodes.clear();
        self.owner = None;
        self.api = None;
    }
}

struct Parameter {
    name: String,
    key: Py<PyString>,
    slot: usize,
    default: Option<Py<PyAny>>,
    factory: Option<Py<PyAny>>,
}

// A retained capture supplies its frame context directly instead of through
// begin_frame globals and reserved keyword arguments.
pub(crate) struct Capture<'a, 'py> {
    pub(crate) unique: u128,
    pub(crate) explicit: &'a Bound<'py, PyAny>,
    // Current rectangle and scroll offset. They overlay the declaration's
    // arguments, which therefore need no per-execution copy.
    pub(crate) rect: (f64, f64, f64, f64),
    pub(crate) auto_height: bool,
    pub(crate) scroll: (f64, f64),
    pub(crate) cache: &'a Bound<'py, PyAny>,
    pub(crate) events: &'a Bound<'py, PyAny>,
    // The caller's capture scope already opened the view's ID scope and
    // measuring group and will close and measure them.
    pub(crate) fused: bool,
}

// Keyword values a body's parameters are looked up in. An immediate call owns
// one merged dictionary. A capture of a function without **kwargs only reads
// declared parameters, so it layers framework values over the declaration's
// arguments and the decorator defaults without copying either.
enum Args<'py> {
    Merged(Bound<'py, PyDict>),
    Layered { own: Bound<'py, PyDict>, kwargs: Bound<'py, PyDict>, defaults: Bound<'py, PyDict> },
}
impl<'py> Args<'py> {
    fn get(&self, key: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        match self {
            Self::Merged(args) => args.get_item(key),
            Self::Layered { own, kwargs, defaults } => {
                if let Some(value) = own.get_item(key)? { return Ok(Some(value)); }
                if let Some(value) = kwargs.get_item(key)? { return Ok(Some(value)); }
                defaults.get_item(key)
            }
        }
    }
    fn contains(&self, key: &Bound<'py, PyAny>) -> PyResult<bool> {
        Ok(match self {
            Self::Merged(args) => args.contains(key)?,
            Self::Layered { own, kwargs, defaults } => own.contains(key)? || kwargs.contains(key)? || defaults.contains(key)?,
        })
    }
    fn set<V: IntoPyObject<'py>>(&self, key: &Bound<'py, PyAny>, value: V) -> PyResult<()> {
        match self {
            Self::Merged(args) => args.set_item(key, value),
            Self::Layered { own, .. } => own.set_item(key, value),
        }
    }
}

// Framework values a layered capture only materializes for parameters that
// actually ask for them by name.
const FRAMEWORK_FIELDS: [&str; 12] = [
    "width", "height", "scroll_offset", "return_extras", "cache", "view_events",
    "left", "top", "input_value", "draw_state", "unique", "depth",
];
const WANT_WIDTH: u16 = 1 << 0;
const WANT_HEIGHT: u16 = 1 << 1;
const WANT_SCROLL: u16 = 1 << 2;
const WANT_EXTRAS: u16 = 1 << 3;
const WANT_CACHE: u16 = 1 << 4;
const WANT_EVENTS: u16 = 1 << 5;
const WANT_LEFT: u16 = 1 << 6;
const WANT_TOP: u16 = 1 << 7;
const WANT_INPUT: u16 = 1 << 8;
const WANT_STATE: u16 = 1 << 9;
const WANT_UNIQUE: u16 = 1 << 10;
const WANT_DEPTH: u16 = 1 << 11;
fn wanted(parameters: &[Parameter]) -> u16 {
    let mut mask = 0;
    for parameter in parameters {
        if let Some(bit) = FRAMEWORK_FIELDS.iter().position(|name| *name == parameter.name) {
            mask |= 1 << bit;
        }
    }
    mask
}

#[pyclass(unsendable, module = "meltygui.core.rendering._gui_native")]
pub(crate) struct Renderer {
    runtime: Py<Runtime>,
    func: Py<PyAny>,
    code: Option<Py<PyAny>>,
    defaults: Py<PyDict>,
    parameters: Vec<Parameter>,
    // FRAMEWORK_FIELDS that are declared parameters, as WANT_* bits.
    wants: u16,
    events: Vec<String>,
    var_kwargs: bool,
    excluded: Vec<String>,
    id: u128,
}

impl Renderer {
    fn invoke(
        &self,
        py: Python<'_>,
        input_value: Option<Py<PyAny>>,
        kwargs: Option<&Bound<'_, PyDict>>,
        capture: Option<Capture<'_, '_>>,
    ) -> PyResult<Py<PyAny>> {
        let start = Instant::now();
        let input_value = input_value.unwrap_or_else(|| py.None());
        // A declaration already rejected window_pos for captures.
        let layered = capture.is_some() && !self.var_kwargs;
        let args = match kwargs {
            Some(kwargs) if layered => Args::Layered {
                own: PyDict::new(py), kwargs: kwargs.clone(), defaults: self.defaults.bind(py).clone() },
            _ => {
                let merged = self.defaults.bind(py).copy()?;
                if let Some(kwargs) = kwargs {
                    merged.update(kwargs.as_mapping())?;
                }
                if merged.contains(intern!(py, "window_pos"))? {
                    return Err(PyTypeError::new_err("gui uses relative left and top, not window_pos"));
                }
                Args::Merged(merged)
            }
        };
        let layered = matches!(args, Args::Layered { .. });
        // A merged dictionary receives every framework value: **kwargs bodies
        // see them all. A layered one only those a parameter asks for.
        let wants = |bit: u16| !layered || self.wants & bit != 0;
        // Cached calls carry defaults for layout; only caller keys outrank state.
        let (captured, explicit_keys, supplied_unique) = match &capture {
            Some(capture) => {
                if wants(WANT_WIDTH) { args.set(intern!(py, "width"), capture.rect.2)?; }
                if wants(WANT_HEIGHT) {
                    if capture.auto_height { args.set(intern!(py, "height"), py.None())?; }
                    else { args.set(intern!(py, "height"), capture.rect.3)?; }
                }
                if wants(WANT_SCROLL) { args.set(intern!(py, "scroll_offset"), capture.scroll)?; }
                if wants(WANT_EXTRAS) { args.set(intern!(py, "return_extras"), true)?; }
                (true, Some(capture.explicit.clone()), Some(capture.unique))
            }
            None => {
                let Args::Merged(merged) = &args else { unreachable!() };
                let captured = merged.contains(intern!(py, "_gui_unique"))?;
                let explicit_keys = merged.get_item(intern!(py, "_gui_explicit"))?;
                if explicit_keys.is_some() { merged.del_item(intern!(py, "_gui_explicit"))?; }
                let unique = match merged.get_item(intern!(py, "_gui_unique"))? {
                    Some(value) => {
                        let unique = value.extract::<u128>()?;
                        merged.del_item(intern!(py, "_gui_unique"))?;
                        Some(unique)
                    }
                    None => None,
                };
                (captured, explicit_keys, unique)
            }
        };
        let (state, unique, unique_value, label, inherited_width, api, owner, globals) = {
            let mut rt = self.runtime.borrow_mut(py);
            // One runtime serves every identity of a retained definition, so a
            // capture can start while an enclosing capture of the same function
            // is still on the stack. Captures never use an event owner.
            if capture.is_some() {
                rt.owner = None;
            }
            let unique = match supplied_unique {
                Some(unique) => unique,
                None => rt.identities.borrow_mut(py).resolve(self.id, args.get(intern!(py, "key"))?.as_ref(),
                    args.get(intern!(py, "name"))?.as_ref(), args.contains(intern!(py, "key"))?)?,
            };
            rt.ensure_state(py, unique)?;
            {
                let mut ds=rt.nodes[&unique].0.borrow_mut(py);
                if ds.renderer != self.id {
                    ds.renderer=self.id;
                    ds.excluded=TRANSIENT_STATE.iter().map(|v|v.to_string()).chain(self.excluded.iter().cloned()).collect();
                }
            }
            let api = rt.api(py)?;
            let (state, unique_value, label) = rt.nodes.get(&unique).unwrap();
            let width = rt.stack.last().map_or(rt.width, |frame| frame.width);
            (
                state.clone_ref(py),
                unique,
                unique_value.clone_ref(py),
                label.clone_ref(py),
                width,
                api,
                rt.owner.as_ref().map(|v| v.clone_ref(py)),
                if capture.is_some() { None } else { Some(rt.globals.clone_ref(py)) },
            )
        };
        if let Some(capture) = &capture {
            if wants(WANT_CACHE) && !args.contains(intern!(py, "cache"))? { args.set(intern!(py, "cache"), capture.cache)?; }
            if wants(WANT_EVENTS) && !args.contains(intern!(py, "view_events"))? { args.set(intern!(py, "view_events"), capture.events)?; }
        } else if let Some(globals) = globals {
            for (key, value) in globals.bind(py).iter() {
                if !args.contains(&key)? {
                    args.set(&key, value)?;
                }
            }
        }
        let number = |key: &Bound<'_, PyAny>, default: f64| -> PyResult<f64> {
            Ok(args.get(key)?.and_then(|v| v.extract().ok()).unwrap_or(default))
        };
        let stored_height = state.borrow(py).at(SLOT_HEIGHT).and_then(Value::number).unwrap_or(24.0);
        // A capture's size is its current rectangle; automatic height keeps
        // the stored extent until the body is measured.
        let (width, height) = match &capture {
            Some(capture) => (capture.rect.2.max(1.0),
                              if capture.auto_height { stored_height } else { capture.rect.3 }.max(1.0)),
            None => (number(intern!(py, "width"), inherited_width)?.max(1.0),
                     number(intern!(py, "height"), stored_height)?.max(1.0)),
        };
        if !captured {
            if let Some(initial) = args.get(intern!(py, "initial"))? {
                let initial = initial.downcast::<PyDict>()?;
                if initial.contains(intern!(py, "window_pos"))? {
                    return Err(PyTypeError::new_err("initial uses left and top, not window_pos"));
                }
                let empty = PyDict::new(py);
                state.borrow_mut(py).configure(&empty, &empty, initial)?;
            }
        }
        // The layout cursor is only consulted for flow placement, an immediate
        // call's cursor move, or event rectangles. Explicit captures skip it.
        let mut cursor_cache: Option<(f64, f64)> = None;
        let mut cursor = || -> PyResult<(f64, f64)> {
            if let Some(cursor) = cursor_cache { return Ok(cursor); }
            let value = match &api {
                Some(api) => api.cursor.bind(py).call0()?.extract::<(f64, f64)>()?,
                None => (0.0, 0.0),
            };
            cursor_cache = Some(value);
            Ok(value)
        };
        let resolve_axis = |key: &Bound<'_, PyString>, name: &str, slot: usize| -> PyResult<Option<f64>> {
            let explicit = kwargs.map(|kw| kw.get_item(key)).transpose()?.flatten();
            let value = match explicit {
                Some(value) => Some(value.unbind()),
                None => {
                    let authored = state.borrow(py).overrides.get(&slot).map(|v| v.to_py(py));
                    match authored {
                        Some(value) => Some(value),
                        None => match args.get(key)? {
                            Some(value) => Some(value.unbind()),
                            None => self.parameters.iter().find(|p| p.name == name)
                                .and_then(|p| p.default.as_ref()).map(|v| v.clone_ref(py)),
                        },
                    }
                }
            };
            match value {
                Some(v) if !v.is_none(py) => {
                    let number = v.extract::<f64>(py)?;
                    if !number.is_finite() {return Err(PyTypeError::new_err("position must be finite"));}
                    Ok(Some(number))
                }
                _ => Ok(None),
            }
        };
        // A capture's position is its current rectangle, always explicit.
        let captured_position = capture.as_ref().map(|capture| (capture.rect.0, capture.rect.1));
        if let Some((left, top)) = captured_position {
            if !left.is_finite() || !top.is_finite() {return Err(PyTypeError::new_err("position must be finite"));}
        }
        let left = match if let Some((left, _)) = captured_position { Some(left) } else { resolve_axis(intern!(py, "left"), "left", SLOT_LEFT)? } {
            Some(value) => value,
            None => {
                let flow = cursor()?.0;
                if !flow.is_finite() {return Err(PyTypeError::new_err("position must be finite"));}
                flow
            }
        };
        let top = match if let Some((_, top)) = captured_position { Some(top) } else { resolve_axis(intern!(py, "top"), "top", SLOT_TOP)? } {
            Some(value) => value,
            None => {
                let flow = cursor()?.1;
                if !flow.is_finite() {return Err(PyTypeError::new_err("position must be finite"));}
                flow
            }
        };
        if !captured && (left, top) != cursor()? {
            if let Some(api) = &api {
                api.set_cursor.bind(py).call1(((left, top),))?;
            }
        }
        if wants(WANT_LEFT) { args.set(intern!(py, "left"), left)?; }
        if wants(WANT_TOP) { args.set(intern!(py, "top"), top)?; }
        {
            let mut ds = state.borrow_mut(py);
            ds.put_slot(SLOT_UNIQUE, Value::Object(unique_value.clone_ref(py)));
            ds.put_slot(SLOT_DLT_COUNT, Value::Int(1));
            ds.put_slot(SLOT_WIDTH, Value::Float(width));
            ds.put_slot(SLOT_HEIGHT, Value::Float(height));
            ds.put_slot(SLOT_LEFT, Value::Float(left));
            ds.put_slot(SLOT_TOP, Value::Float(top));
            ds.put_slot(SLOT_CONTENT_WIDTH, Value::Float(width));
            ds.put_slot(SLOT_CONTENT_HEIGHT, Value::Float(height));
        }
        if wants(WANT_INPUT) { args.set(intern!(py, "input_value"), &input_value)?; }
        if wants(WANT_STATE) { args.set(intern!(py, "draw_state"), &state)?; }
        if wants(WANT_UNIQUE) { args.set(intern!(py, "unique"), &unique_value)?; }
        // Each capture is its own root: depth counts immediate nesting only.
        if wants(WANT_DEPTH) { args.set(intern!(py, "depth"), if capture.is_some() { 0 } else { self.runtime.borrow(py).stack.len() })?; }
        if !self.events.is_empty() {
            if let Some(owner) = owner {
                let event_args = PyDict::new(py);
                event_args.set_item("view_id", &label)?;
                let (x,y) = if captured {cursor()?} else {(left,top)};
                event_args.set_item("rect", (x, y, x + width, y + height))?;
                event_args.set_item("priority_delta", 0)?;
                let events =
                    owner
                        .bind(py)
                        .call_method("on_action", (&self.events,), Some(&event_args))?;
                if let Ok(events) = events.downcast::<PyDict>() {
                    for (name, value) in events.iter() {
                        if kwargs.map_or(true, |kw| !kw.contains(&name).unwrap_or(false)) {
                            args.set(&name, value)?;
                        }
                    }
                }
            }
        }
        for name in &self.events {
            let name = PyString::new(py, name);
            if !args.contains(&name)? {
                args.set(&name, py.None())?;
            }
        }
        let clean = match &args {
            Args::Merged(merged) if self.var_kwargs => merged.copy()?,
            _ => PyDict::new(py),
        };
        for parameter in &self.parameters {
            let key = parameter.key.bind(py);
            let position = matches!(parameter.name.as_str(), "left" | "top");
            // A capture's size and scroll come from its overlay, not the
            // declaration's stored arguments.
            let overlaid = capture.is_some() && matches!(parameter.name.as_str(), "width" | "height" | "scroll_offset");
            let explicit = if position { None } else {
                match if overlaid { args.get(key)? } else { kwargs.map(|kw| kw.get_item(key)).transpose()?.flatten() } {
                    Some(value) if explicit_keys.as_ref().map_or(true, |keys| keys.contains(key).unwrap_or(false)) => Some(value),
                    _ => None,
                }
            };
            // Authored state is only consulted (and converted) when no caller
            // value outranks it. The borrow ends before any later branch.
            let authored = if position || explicit.is_some() { None } else {
                let ds = state.borrow(py);
                ds.overrides.get(&parameter.slot).map(|v| v.to_py(py))
            };
            let value = if position {
                args.get(key)?.unwrap().unbind()
            } else if let Some(value) = explicit {
                value.unbind()
            } else if let Some(value) = authored {
                value
            } else if let Some(value) = args.get(key)? {
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
            clean.set_item(key, &value)?;
            // Framework-injected self references are transient, not persisted in state.
            // None requests automatic sizing; it is not a measured dimension.
            let automatic_size = matches!(parameter.name.as_str(), "width" | "height")
                && value.bind(py).is_none();
            if parameter.name != "draw_state" && parameter.name != "input_value" && !automatic_size {
                state
                    .borrow_mut(py)
                    .put_slot(parameter.slot, Value::field(&parameter.name, value.bind(py))?);
            }
        }
        let fused = capture.as_ref().is_some_and(|capture| capture.fused);
        if let (Some(api), false) = (&api, fused) {
            api.push_id.bind(py).call1((&label,))?;
            if let Err(error) = api.begin_group.bind(py).call0() {
                api.pop_id.bind(py).call0()?;
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
            if let (Some(api), false) = (&api, fused) {
                let end = api.end_group.bind(py).call0();
                let pop = api.pop_id.bind(py).call0();
                end?;
                pop?;
                let (measured_w, measured_h) = api.item_rect_size.bind(py)
                    .call0()?
                    .extract::<(f64, f64)>()?;
                let mut ds = state.borrow_mut(py);
                ds.put_slot(SLOT_CONTENT_HEIGHT, Value::Float(measured_h));
                if ds.observer.is_none() {
                    let fixed = match &capture {
                        Some(capture) => (!capture.auto_height).then_some(capture.rect.3),
                        None => args.get(intern!(py, "height"))?.and_then(|v| v.extract().ok()),
                    };
                    ds.put_slot(SLOT_HEIGHT, Value::Float(fixed.unwrap_or(measured_h)));
                }
                ds.put_slot(SLOT_MEASURED_WIDTH, Value::Float(measured_w));
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
            if capture.is_some() { rt.owner = None; }
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
        if capture.is_some() {
            // Also report whether the state carries a tint, so the common
            // untinted view skips a separate lookup.
            let result = result.bind(py).downcast::<PyTuple>()?;
            let tinted = state.borrow(py).lookup("tint").is_some();
            return Ok((result.get_item(0)?, result.get_item(1)?, state, tinted)
                .into_pyobject(py)?
                .into_any()
                .unbind());
        }
        if args
            .get(intern!(py, "return_extras"))?
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
}

#[pymethods]
impl Renderer {
    fn reconfigure(&mut self, py: Python<'_>, replacement: &Renderer) {
        self.func = replacement.func.clone_ref(py);
        self.code = replacement.code.as_ref().map(|v| v.clone_ref(py));
        self.defaults = replacement.defaults.clone_ref(py);
        self.events = replacement.events.clone();
        self.var_kwargs = replacement.var_kwargs;
        self.excluded = replacement.excluded.clone();
        for (state, _, _) in self.runtime.borrow(py).nodes.values() {
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
                key: p.key.clone_ref(py),
                slot: p.slot,
                default: p.default.as_ref().map(|v| v.clone_ref(py)),
                factory: p.factory.as_ref().map(|v| v.clone_ref(py)),
            })
            .collect();
        self.wants = replacement.wants;
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
        let plan: Vec<Parameter> = {
            let rt = runtime.borrow(py);
            let plan = parameters
                .into_iter()
                .map(|(name, has_default, default, factory)| {
                    let slot = rt.shape.borrow_mut().slot(&name);
                    Parameter {
                        key: PyString::intern(py, &name).unbind(),
                        name,
                        slot,
                        default: has_default.then_some(default),
                        factory,
                    }
                })
                .collect();
            plan
        };
        let code = func.bind(py).getattr(intern!(py, "__code__")).ok().map(Bound::unbind);
        Ok(Self {
            runtime,
            func,
            code,
            defaults,
            wants: wanted(&plan),
            parameters: plan,
            events,
            var_kwargs,
            excluded,
            id: identity,
        })
    }
    /// The function code this parameter plan was compiled from.
    #[getter]
    fn code(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.code.as_ref().map(|v| v.clone_ref(py))
    }
    #[pyo3(signature = (input_value=None, **kwargs))]
    fn __call__(
        &self,
        py: Python<'_>,
        input_value: Option<Py<PyAny>>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        self.invoke(py, input_value, kwargs, None)
    }
    /// One retained capture. The declaration's arguments are read, not
    /// copied; `rect`, `auto_height` and `scroll` supply the current geometry
    /// and `explicit` the caller's keys. The frame context arrives directly.
    /// Returns (changed, value, state, whether the state has a tint).
    #[allow(clippy::too_many_arguments)]
    fn execute(
        &self,
        py: Python<'_>,
        input_value: Py<PyAny>,
        arguments: &Bound<'_, PyDict>,
        rect: (f64, f64, f64, f64),
        auto_height: bool,
        scroll: (f64, f64),
        unique: u128,
        explicit: &Bound<'_, PyAny>,
        cache: &Bound<'_, PyAny>,
        view_events: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        self.invoke(py, Some(input_value), Some(arguments),
                    Some(Capture { unique, explicit, rect, auto_height, scroll, cache, events: view_events, fused: false }))
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.runtime)?;
        visit.call(&self.func)?;
        if let Some(code) = &self.code {
            visit.call(code)?;
        }
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

/// Whether two keyword dictionaries hold the same keys, in the same order,
/// with values that are identical objects or equal exact primitives. Never
/// compares or hashes arbitrary objects.
#[pyfunction]
fn same_arguments(a: &Bound<'_, PyDict>, b: &Bound<'_, PyDict>) -> PyResult<bool> {
    if a.len() != b.len() { return Ok(false); }
    for ((key_a, value_a), (key_b, value_b)) in a.iter().zip(b.iter()) {
        if !key_a.is(&key_b) && !key_a.eq(&key_b)? { return Ok(false); }
        if !retained::same_argument(&value_a, &value_b)? { return Ok(false); }
    }
    Ok(true)
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
    m.add_class::<declare::Declarations>()?;
    m.add_class::<executor::Executor>()?;
    m.add_function(wrap_pyfunction!(same_arguments, m)?)?;
    Ok(())
}
