//! Stable identities are independent of values, layout and runtime allocation order.
//! v2 uses typed, length-delimited little-endian fields and the first 16 SHA-256
//! bytes as a little-endian integer. Hash only on cache misses; no collision checks.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyString, PyTuple};
use sha2::{Digest, Sha256};
use crate::hash::{Map as HashMap, Set as HashSet};

#[derive(Hash, Eq, PartialEq)]
enum Key {
    None,
    Int(i128),
    Text(String),
    Tuple(Vec<Key>),
}
impl Key {
    fn read(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        if value.is_none() {
            return Ok(Self::None);
        }
        if value.is_exact_instance_of::<PyInt>() {
            return Ok(Self::Int(value.extract()?));
        }
        if value.is_exact_instance_of::<PyString>() {
            return Ok(Self::Text(value.extract()?));
        }
        if value.is_exact_instance_of::<PyTuple>() {
            return Ok(Self::Tuple(
                value
                    .downcast::<PyTuple>()?
                    .iter()
                    .map(|v| Self::read(&v))
                    .collect::<PyResult<_>>()?,
            ));
        }
        Err(PyTypeError::new_err(
            "gui identity keys must be None, str, signed 128-bit int, or tuples of these",
        ))
    }
    fn encode(&self, hash: &mut Sha256) {
        match self {
            Self::None => hash.update([0]),
            Self::Int(n) => {
                hash.update([1]);
                hash.update(n.to_le_bytes());
            }
            Self::Text(s) => {
                hash.update([2]);
                hash.update((s.len() as u64).to_le_bytes());
                hash.update(s.as_bytes());
            }
            Self::Tuple(items) => {
                hash.update([3]);
                hash.update((items.len() as u64).to_le_bytes());
                for item in items {
                    item.encode(hash);
                }
            }
        }
    }
}

#[derive(Hash, Eq, PartialEq)]
enum Discriminator {
    Key(Key),
    Name(String),
    Occurrence(u64),
}
impl Discriminator {
    fn encode(&self, hash: &mut Sha256) {
        match self {
            Self::Key(key) => {
                hash.update([0]);
                key.encode(hash);
            }
            Self::Name(name) => {
                hash.update([1]);
                hash.update(name.as_bytes());
            }
            Self::Occurrence(n) => {
                hash.update([2]);
                hash.update(n.to_le_bytes());
            }
        }
    }
}

#[derive(Default)]
struct Scope {
    unique: u128,
    counts: HashMap<u128, u64>,
    children: Vec<u128>,
}

fn finish(hash: Sha256) -> u128 {
    u128::from_le_bytes(hash.finalize()[..16].try_into().unwrap())
}

#[pyclass]
pub struct Identities {
    keys: HashMap<Key, u128>,
    edges: HashMap<(u128, u128, Discriminator), usize>,
    uniques: Vec<u128>,
    scopes: Vec<Scope>,
    depth: usize,
    root_counts: HashMap<u128, u64>,
    root: u128,
    roots: Vec<u128>,
    children: HashMap<u128, Vec<u128>>,
    pub registry: Py<PyDict>,
    pub persistent: bool,
    pub states: HashSet<u128>,
}
impl Identities {
    fn token(&mut self, key: Key) -> u128 {
        *self.keys.entry(key).or_insert_with_key(|key| {
            let mut hash = Sha256::new();
            hash.update(b"melty-gui-key-v1");
            key.encode(&mut hash);
            finish(hash)
        })
    }
}
#[pymethods]
impl Identities {
    #[getter]
    fn registry(&self, py: Python<'_>) -> Py<PyDict> {
        self.registry.clone_ref(py)
    }
    #[new]
    #[pyo3(signature = (registry=None))]
    pub fn new(py: Python<'_>, registry: Option<Py<PyDict>>) -> Self {
        let mut ids = Self {
            keys: HashMap::default(),
            edges: HashMap::default(),
            uniques: vec![],
            scopes: vec![],
            depth: 0,
            root_counts: HashMap::default(),
            root: 0,
            roots: vec![],
            children: HashMap::default(),
            persistent: registry.is_some(),
            registry: registry.unwrap_or_else(|| PyDict::new(py).unbind()),
            states: HashSet::default(),
        };
        ids.root = ids.token(Key::None);
        ids
    }
    pub fn clear(&mut self, py: Python<'_>) {
        self.keys.clear();
        self.edges.clear();
        self.uniques.clear();
        self.scopes.clear();
        self.root_counts.clear();
        self.depth = 0;
        self.roots.clear();
        self.children.clear();
        self.states.clear();
        if !self.persistent {
            self.registry.bind(py).clear();
        }
    }
    #[getter]
    pub fn active(&self) -> bool {
        self.depth != 0
    }
    pub fn begin_frame(&mut self) {
        if self.depth == 0 {
            self.root_counts.clear();
            self.roots.clear();
        }
    }
    pub fn enter(&mut self, unique: u128) {
        // Reuse depth slots so redraws retain the counter maps' capacity.
        if self.depth == self.scopes.len() {
            self.scopes.push(Scope::default());
        }
        let scope = &mut self.scopes[self.depth];
        scope.unique = unique;
        scope.counts.clear();
        scope.children.clear();
        self.depth += 1;
    }
    pub fn leave(&mut self, success: bool) {
        self.depth -= 1;
        if success {
            let scope = &mut self.scopes[self.depth];
            std::mem::swap(
                self.children.entry(scope.unique).or_default(),
                &mut scope.children,
            );
        }
    }
    pub fn prepare_save(&mut self, py: Python<'_>) -> PyResult<()> {
        let mut active = HashSet::default();
        let mut pending = self.roots.clone();
        while let Some(id) = pending.pop() {
            if active.insert(id) {
                if let Some(children) = self.children.get(&id) {
                    pending.extend(children);
                }
            }
        }
        for id in &self.states {
            if let Some(value) = self.registry.bind(py).get_item(id)? {
                if let Ok(state) = value.extract::<Py<super::NativeState>>() {
                    state.borrow_mut(py).put(
                        "dlt_count",
                        super::Value::Int(if active.contains(id) { 1 } else { 0 }),
                    );
                }
            }
        }
        Ok(())
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.registry)
    }
    fn __clear__(&mut self, py: Python<'_>) {
        // Detach our reference; an app session may still own this registry.
        self.registry = PyDict::new(py).unbind();
    }

    pub fn set_scope(&mut self, scope: &Bound<'_, PyAny>) -> PyResult<()> {
        self.root = self.token(Key::read(scope)?);
        Ok(())
    }
    pub fn renderer(&mut self, name: String) -> u128 {
        self.token(Key::Text(name))
    }
    #[pyo3(signature = (renderer, key=None, name=None, keyed=false))]
    pub fn handle(
        &mut self,
        renderer: u128,
        key: Option<&Bound<'_, PyAny>>,
        name: Option<&Bound<'_, PyAny>>,
        keyed: bool,
    ) -> PyResult<usize> {
        let (parent, counts) = if self.depth == 0 {
            (self.root, &mut self.root_counts)
        } else {
            let scope = &mut self.scopes[self.depth - 1];
            (scope.unique, &mut scope.counts)
        };
        let name = if keyed {
            None
        } else {
            name.filter(|n| !n.is_none())
                .map(|n| n.extract::<String>())
                .transpose()?
        };
        let key = if keyed {
            Discriminator::Key(key.map(Key::read).transpose()?.unwrap_or(Key::None))
        } else if let Some(name) = name.filter(|n| !n.is_empty()) {
            Discriminator::Name(name)
        } else {
            let count = counts.entry(renderer).or_default();
            let ordinal = *count;
            *count += 1;
            Discriminator::Occurrence(ordinal)
        };
        let handle = *self
            .edges
            .entry((parent, renderer, key))
            .or_insert_with_key(|(_, _, key)| {
                let mut hash = Sha256::new();
                hash.update(b"melty-gui-view-v2");
                hash.update(parent.to_le_bytes());
                hash.update(renderer.to_le_bytes());
                key.encode(&mut hash);
                let handle = self.uniques.len();
                self.uniques.push(finish(hash));
                handle
            });
        let unique = self.uniques[handle];
        if self.depth == 0 {
            self.roots.push(unique);
        } else {
            self.scopes[self.depth - 1].children.push(unique);
        }
        Ok(handle)
    }
    pub fn unique(&self, handle: usize) -> PyResult<u128> {
        self.uniques
            .get(handle)
            .copied()
            .ok_or_else(|| PyTypeError::new_err("unknown gui identity handle"))
    }
    #[pyo3(signature = (renderer, key=None, name=None, keyed=false))]
    pub fn resolve(
        &mut self,
        renderer: u128,
        key: Option<&Bound<'_, PyAny>>,
        name: Option<&Bound<'_, PyAny>>,
        keyed: bool,
    ) -> PyResult<u128> {
        let handle = self.handle(renderer, key, name, keyed)?;
        Ok(self.uniques[handle])
    }
}
