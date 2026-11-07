//! Experimental call forwarding only: no state, parameter plan or rendering host.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

/// Fixed benchmark body signature only. No signature inspection or injection.
#[pyfunction]
pub fn fixed_body(py: Python<'_>, function: Py<PyAny>, input: Py<PyAny>,
                  arguments: &Bound<'_, PyDict>, state: Py<PyAny>) -> PyResult<Py<PyAny>> {
    let left = arguments.get_item("left")?.unwrap_or_else(|| 0_f64.into_pyobject(py).unwrap().into_any());
    let top = arguments.get_item("top")?.unwrap_or_else(|| 0_f64.into_pyobject(py).unwrap().into_any());
    let width = arguments.get_item("width")?.ok_or_else(|| pyo3::exceptions::PyValueError::new_err("width required"))?;
    let height = arguments.get_item("height")?.ok_or_else(|| pyo3::exceptions::PyValueError::new_err("height required"))?;
    let value = function.bind(py).call1((input, left, top, width, height, py.None()))?;
    let result = value.downcast::<PyTuple>()?;
    Ok((result.get_item(0)?, result.get_item(1)?, state).into_pyobject(py)?.into_any().unbind())
}

#[pyclass]
pub struct BareGui {
    function: Option<Py<PyAny>>,
}

#[pymethods]
impl BareGui {
    #[new]
    fn new(function: Py<PyAny>) -> Self {
        Self { function: Some(function) }
    }

    #[pyo3(signature = (input_value, **kwargs))]
    fn __call__(&self, py: Python<'_>, input_value: Py<PyAny>,
                kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<Py<PyAny>> {
        let function = self.function.as_ref().ok_or_else(||
            pyo3::exceptions::PyRuntimeError::new_err("Cleared bare wrapper"))?;
        Ok(function.bind(py).call((input_value,), kwargs)?.unbind())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(function) = &self.function { visit.call(function)?; }
        Ok(())
    }

    fn __clear__(&mut self) { self.function = None; }
}
