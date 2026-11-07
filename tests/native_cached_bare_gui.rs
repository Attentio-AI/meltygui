//! Explicit-invalidation cache controller, with no parameter plan or draw state.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashMap;
use std::sync::Mutex;
use std::thread::{self, ThreadId};

#[derive(Default)]
struct Records {
    generation: u64,
    depth: usize,
    values: HashMap<u64, (u64, Py<PyAny>)>,
}

#[pyclass]
pub struct CachedBareGui {
    function: Option<Py<PyAny>>,
    bridge: Option<Py<PyAny>>,
    owner: ThreadId,
    records: Mutex<Records>,
}

#[pymethods]
impl CachedBareGui {
    #[new]
    fn new(function: Py<PyAny>, bridge: Py<PyAny>) -> Self {
        Self { function: Some(function), bridge: Some(bridge),
               owner: thread::current().id(), records: Mutex::new(Records::default()) }
    }

    fn invalidate(&self) {
        self.records.lock().unwrap().generation += 1;
    }

    fn clear(&self) {
        let old = {
            let mut records = self.records.lock().unwrap();
            records.generation += 1;
            std::mem::take(&mut records.values)
        };
        drop(old);
    }

    fn count(&self) -> usize { self.records.lock().unwrap().values.len() }

    #[pyo3(signature = (input_value, **kwargs))]
    fn __call__(&self, py: Python<'_>, input_value: Py<PyAny>,
                kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<Py<PyAny>> {
        if thread::current().id() != self.owner {
            return Err(PyRuntimeError::new_err("Cached drawing must run on its owning thread"));
        }
        let function = self.function.as_ref().ok_or_else(|| PyRuntimeError::new_err("Cleared wrapper"))?;
        let bridge = self.bridge.as_ref().ok_or_else(|| PyRuntimeError::new_err("Cleared bridge"))?.bind(py);
        let kwargs = kwargs.ok_or_else(|| PyValueError::new_err("Explicit key and geometry required"))?;
        let id = kwargs.get_item("key")?.ok_or_else(|| PyValueError::new_err("Explicit key required"))?.extract::<u64>()?;
        let (generation, root, cached) = {
            let mut records = self.records.lock().unwrap();
            let generation = records.generation;
            let cached = records.values.get(&id).filter(|(g, _)| *g == generation)
                .map(|(_, value)| value.clone_ref(py));
            let root = records.depth == 0;
            records.depth += 1;
            (generation, root, cached)
        };
        // No lock or mutable Rust borrow survives a Python callback or destructor.
        let dirty = cached.is_none();
        let outcome = (|| {
            let value = match cached {
                Some(value) => value,
                None => {
                    let value = bridge.call_method1("capture", (id, root, function, input_value, kwargs))?.unbind();
                    let old = self.records.lock().unwrap().values.insert(id, (generation, value.clone_ref(py)));
                    drop(old);
                    value
                }
            };
            bridge.call_method1("place", (id, kwargs, root, dirty))?;
            Ok(value)
        })();
        {
            let mut records = self.records.lock().unwrap();
            records.depth -= 1;
            if outcome.is_err() { records.generation += 1; }
        }
        outcome
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(function) = &self.function { visit.call(function)?; }
        if let Some(bridge) = &self.bridge { visit.call(bridge)?; }
        let records = self.records.lock().unwrap();
        for (_, value) in records.values.values() { visit.call(value)?; }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.function = None;
        self.bridge = None;
        self.records.get_mut().unwrap().values.clear();
    }
}
