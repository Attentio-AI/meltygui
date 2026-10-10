//! Explicit invalidation and ownership, independent of rasterization.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyTuple};
use std::collections::{HashMap, HashSet};

struct Node {
    identity: Py<PyAny>,
    parent: u64,
    children: Vec<u64>,
    declared: Option<Vec<u64>>,
    input: Py<PyAny>,
    caller_input: Py<PyAny>,
    kwargs: Py<PyDict>,
    value: Py<PyAny>,
    pending: bool,
    dirty: bool,
    compose: bool,
    portal: bool,
    rect: (f32, f32, f32, f32),
    constraints: (f32, f32), // negative height requests content measurement
    layout_changes: u64,
    z: u64,
    executions: u64,
    aliases: Vec<String>,
    anchors: Vec<Py<PyAny>>,
}

fn same_argument(a: &Bound<'_, PyAny>, b: &Bound<'_, PyAny>) -> PyResult<bool> {
    if a.is(b) {
        return Ok(true);
    }
    if !a.get_type().is(&b.get_type()) {
        return Ok(false);
    }
    if a.is_exact_instance_of::<PyInt>()
        || a.is_exact_instance_of::<PyFloat>()
        || a.is_exact_instance_of::<PyBool>()
        || a.is_exact_instance_of::<PyString>()
    {
        return a.eq(b);
    }
    Ok(false)
}

#[pyclass(unsendable)]
pub struct RetainedGraph {
    identities: Py<PyDict>,
    nodes: HashMap<u64, Node>,
    serial: u64,
    rank: u64,
    aliases: HashMap<String, HashSet<u64>>,
    retired: Vec<u64>,
}

impl RetainedGraph {
    fn composition(&mut self, mut id: u64) {
        while let Some(node) = self.nodes.get_mut(&id) {
            node.compose = true;
            if node.portal {
                break;
            }
            id = node.parent;
        }
    }
    fn remove_node(&mut self, py: Python<'_>, id: u64) -> PyResult<()> {
        if let Some(n) = self.nodes.remove(&id) {
            for child in n.children {
                self.remove_node(py, child)?;
            }
            self.identities.bind(py).del_item(n.identity)?;
            for alias in n.aliases {
                if let Some(ids) = self.aliases.get_mut(&alias) {
                    ids.remove(&id);
                    if ids.is_empty() {
                        self.aliases.remove(&alias);
                    }
                }
            }
            self.retired.push(id);
        }
        Ok(())
    }
    fn depth(&self, mut id: u64) -> usize {
        let mut depth = 0;
        while let Some(n) = self.nodes.get(&id) {
            depth += 1;
            id = n.parent;
        }
        depth
    }
}

#[pymethods]
impl RetainedGraph {
    #[new]
    fn new(py: Python<'_>) -> Self {
        Self {
            identities: PyDict::new(py).unbind(),
            nodes: HashMap::new(),
            serial: 0,
            rank: 0,
            aliases: HashMap::new(),
            retired: vec![],
        }
    }
    #[allow(clippy::too_many_arguments)]
    fn declare(
        &mut self,
        py: Python<'_>,
        parent: u64,
        renderer: u64,
        key: Py<PyAny>,
        input: Py<PyAny>,
        kwargs: Py<PyDict>,
        rect: (f32, f32, f32, f32),
        portal: bool,
    ) -> PyResult<u64> {
        if parent != 0 && !self.nodes.contains_key(&parent) {
            return Err(PyRuntimeError::new_err("missing owner"));
        }
        let identity = (parent, renderer, key).into_pyobject(py)?.into_any();
        let id = if let Some(id) = self.identities.bind(py).get_item(&identity)? {
            id.extract()?
        } else {
            self.serial += 1;
            self.rank += 1;
            let id = self.serial;
            self.identities.bind(py).set_item(&identity, id)?;
            self.nodes.insert(
                id,
                Node {
                    identity: identity.unbind(),
                    parent,
                    children: vec![],
                    declared: None,
                    input: input.clone_ref(py),
                    caller_input: input.clone_ref(py),
                    kwargs: kwargs.clone_ref(py),
                    value: input.clone_ref(py),
                    pending: false,
                    dirty: true,
                    compose: true,
                    portal,
                    rect: (rect.0, rect.1, rect.2, rect.3.max(0.0)),
                    constraints: (rect.2, rect.3),
                    layout_changes: 0,
                    z: self.rank,
                    executions: 0,
                    aliases: vec![],
                    anchors: vec![],
                },
            );
            id
        };
        if parent != 0 {
            let owner = self.nodes.get_mut(&parent).unwrap();
            let children = owner
                .declared
                .as_mut()
                .ok_or_else(|| PyRuntimeError::new_err("declare outside owner execution"))?;
            if children.contains(&id) {
                return Err(PyRuntimeError::new_err(
                    "duplicate @gui key in one owner execution",
                ));
            }
            children.push(id);
        }
        // Argument change detection is supplied by the bridge: identity/scalar checks,
        // never content hashing or inspection of arbitrary mutable Python objects.
        let node = self.nodes.get_mut(&id).unwrap();
        if node.portal != portal || node.constraints != (rect.2, rect.3) {
            node.dirty = true;
        }
        node.constraints = (rect.2, rect.3);
        node.portal = portal;
        if !portal {
            node.rect.0 = rect.0;
            node.rect.1 = rect.1;
        }
        if !node.pending {
            // A caller can echo its pre-edit scalar while processing a returned
            // edit. Keep the live value until that caller actually supplies a
            // different value; pixel-only redraws must not resurrect the echo.
            if !same_argument(input.bind(py), node.caller_input.bind(py))? {
                node.input = input.clone_ref(py);
                node.caller_input = input;
            }
        }
        // A pending returned edit protects the value, not current layout/style
        // arguments. Resizing while consuming an edit must use the new width.
        node.kwargs = kwargs;
        Ok(id)
    }
    fn invocation(&self, py: Python<'_>, id: u64) -> PyResult<(Py<PyAny>, Py<PyDict>)> {
        let n = self
            .nodes
            .get(&id)
            .ok_or_else(|| PyRuntimeError::new_err("retired view"))?;
        Ok((n.input.clone_ref(py), n.kwargs.clone_ref(py)))
    }
    fn begin(&mut self, id: u64) -> PyResult<()> {
        let n = self
            .nodes
            .get_mut(&id)
            .ok_or_else(|| PyRuntimeError::new_err("retired view"))?;
        if n.declared.is_some() {
            return Err(PyRuntimeError::new_err("recursive retained-node execution"));
        }
        n.declared = Some(vec![]);
        n.dirty = false;
        Ok(())
    }
    fn abort(&mut self, py: Python<'_>, id: u64) -> PyResult<()> {
        if let Some(n) = self.nodes.get_mut(&id) {
            n.dirty = true;
            let staged = n.declared.take().unwrap_or_default();
            let abandoned: Vec<_> = staged
                .into_iter()
                .filter(|c| !n.children.contains(c))
                .collect();
            for child in abandoned {
                self.remove_node(py, child)?;
            }
        }
        Ok(())
    }
    fn commit(
        &mut self,
        py: Python<'_>,
        id: u64,
        result: &Bound<'_, PyTuple>,
        independent: bool,
    ) -> PyResult<()> {
        let changed = result.get_item(0)?.is_truthy()?;
        let n = self
            .nodes
            .get_mut(&id)
            .ok_or_else(|| PyRuntimeError::new_err("retired view"))?;
        let fresh = n
            .declared
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("commit without begin"))?;
        let removed: Vec<_> = n
            .children
            .iter()
            .filter(|c| !fresh.contains(c))
            .copied()
            .collect();
        n.children = fresh;
        n.executions += 1;
        n.value = result.get_item(1)?.unbind();
        if changed {
            n.input = n.value.clone_ref(py);
        }
        n.pending |= changed;
        let parent = n.parent;
        for child in removed {
            self.remove_node(py, child)?;
        }
        self.composition(id);
        // Replay returned edits through the actual caller. Pixel-only invalidation
        // does not execute ancestors; data return propagation deliberately can.
        if changed && independent {
            if let Some(p) = self.nodes.get_mut(&parent) {
                p.dirty = true;
            }
        }
        Ok(())
    }
    fn result(&mut self, py: Python<'_>, id: u64, consume: bool) -> PyResult<(bool, Py<PyAny>)> {
        let n = self
            .nodes
            .get_mut(&id)
            .ok_or_else(|| PyRuntimeError::new_err("retired view"))?;
        let result = (n.pending, n.value.clone_ref(py));
        if consume {
            n.pending = false;
        }
        Ok(result)
    }
    fn dirty(&self, id: u64) -> bool {
        self.nodes.get(&id).is_some_and(|n| n.dirty)
    }
    fn measured(&mut self, id: u64, width: f32, height: f32) -> PyResult<bool> {
        if !width.is_finite() || !height.is_finite() || width <= 0.0 || height < 0.0 {
            return Err(PyRuntimeError::new_err("invalid measured extent"));
        }
        let n = self
            .nodes
            .get_mut(&id)
            .ok_or_else(|| PyRuntimeError::new_err("retired view"))?;
        if (n.rect.2, n.rect.3) == (width, height) {
            return Ok(false);
        }
        n.rect.2 = width;
        n.rect.3 = height;
        n.layout_changes += 1;
        let parent = n.parent;
        let portal = n.portal;
        self.composition(id);
        // Only the immediate layout consumer must execute. A currently executing
        // owner consumes the new extent at its call site. Portals occupy no space.
        if !portal {
            if let Some(owner) = self.nodes.get_mut(&parent) {
                if owner.declared.is_none() {
                    owner.dirty = true;
                }
            }
        }
        Ok(true)
    }
    fn pending(&self, id: u64) -> bool {
        self.nodes.get(&id).is_some_and(|n| n.pending)
    }
    fn invalidate_node(&mut self, id: u64) {
        if let Some(n) = self.nodes.get_mut(&id) {
            n.dirty = true;
        }
    }
    fn associate(&mut self, id: u64, key: String, anchor: Py<PyAny>) {
        if let Some(n) = self.nodes.get_mut(&id) {
            if !n.aliases.contains(&key) {
                n.aliases.push(key.clone());
                n.anchors.push(anchor);
                self.aliases.entry(key).or_default().insert(id);
            }
        }
    }
    fn dissociate(&mut self, id: u64, key: &str) {
        if let Some(n) = self.nodes.get_mut(&id) {
            if let Some(index) = n.aliases.iter().position(|a| a == key) {
                n.aliases.remove(index);
                n.anchors.remove(index);
            }
        }
        if let Some(ids) = self.aliases.get_mut(key) {
            ids.remove(&id);
            if ids.is_empty() {
                self.aliases.remove(key);
            }
        }
    }
    #[pyo3(signature=(key,exclude=0))]
    fn invalidate(&mut self, key: &str, exclude: u64) -> usize {
        let ids = self.aliases.get(key).cloned().unwrap_or_default();
        let mut count = 0;
        for id in ids {
            if id != exclude {
                self.invalidate_node(id);
                count += 1;
            }
        }
        count
    }
    fn dirty_nodes(&self) -> Vec<u64> {
        let mut ids: Vec<_> = self
            .nodes
            .iter()
            .filter(|(_, n)| n.dirty)
            .map(|(id, _)| *id)
            .collect();
        ids.sort_by_key(|id| (self.depth(*id), *id));
        ids
    }
    fn composition_order(&self) -> Vec<u64> {
        let mut ids: Vec<_> = self
            .nodes
            .iter()
            .filter(|(_, n)| n.compose && n.executions > 0)
            .map(|(id, _)| *id)
            .collect();
        ids.sort_by_key(|id| (std::cmp::Reverse(self.depth(*id)), *id));
        ids
    }
    fn composed(&mut self, id: u64) {
        if let Some(n) = self.nodes.get_mut(&id) {
            n.compose = false;
        }
    }
    fn move_window(&mut self, id: u64, x: f32, y: f32) {
        if let Some(n) = self.nodes.get_mut(&id) {
            if n.portal {
                n.rect.0 = x;
                n.rect.1 = y;
            }
        }
    }
    fn allocate(&mut self, py: Python<'_>, id: u64, rect: (f32, f32, f32, f32)) -> PyResult<bool> {
        if ![rect.0, rect.1, rect.2, rect.3]
            .iter()
            .all(|x| x.is_finite())
            || rect.2 <= 0.
            || rect.3 < 0.
        {
            return Err(PyRuntimeError::new_err("invalid layout allocation"));
        }
        let n = self
            .nodes
            .get_mut(&id)
            .ok_or_else(|| PyRuntimeError::new_err("retired layout child"))?;
        if n.rect == rect {
            return Ok(false);
        }
        let resized = (n.rect.2, n.rect.3) != (rect.2, rect.3);
        n.rect = rect;
        if resized {
            n.constraints = (rect.2, rect.3);
            n.kwargs.bind(py).set_item("width", rect.2)?;
            n.kwargs.bind(py).set_item("height", rect.3)?;
            n.dirty = true;
        }
        self.composition(id);
        Ok(true)
    }
    fn raise_window(&mut self, id: u64) {
        self.rank += 1;
        if let Some(n) = self.nodes.get_mut(&id) {
            n.z = self.rank;
        }
    }
    fn info(&self, py: Python<'_>, id: u64) -> PyResult<Py<PyDict>> {
        let n = self
            .nodes
            .get(&id)
            .ok_or_else(|| PyRuntimeError::new_err("retired view"))?;
        let d = PyDict::new(py);
        d.set_item("parent", n.parent)?;
        d.set_item("rect", n.rect)?;
        d.set_item("portal", n.portal)?;
        d.set_item("executions", n.executions)?;
        d.set_item("constraints", n.constraints)?;
        d.set_item("layout_changes", n.layout_changes)?;
        d.set_item("children", &n.children)?;
        d.set_item("z", n.z)?;
        Ok(d.unbind())
    }
    fn nodes(&self) -> Vec<u64> {
        let mut ids: Vec<_> = self.nodes.keys().copied().collect();
        ids.sort();
        ids
    }
    fn windows(&self) -> Vec<u64> {
        let mut ids: Vec<_> = self
            .nodes
            .iter()
            .filter(|(_, n)| n.portal)
            .map(|(id, _)| *id)
            .collect();
        ids.sort_by_key(|id| self.nodes[id].z);
        ids
    }
    fn take_retired(&mut self) -> Vec<u64> {
        std::mem::take(&mut self.retired)
    }
    fn retire(&mut self, py: Python<'_>, id: u64) -> PyResult<()> {
        self.remove_node(py, id)
    }
    fn clear(&mut self, py: Python<'_>) {
        self.retired.extend(self.nodes.keys().copied());
        self.nodes.clear();
        self.aliases.clear();
        self.identities = PyDict::new(py).unbind();
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.identities)?;
        for n in self.nodes.values() {
            visit.call(&n.identity)?;
            visit.call(&n.input)?;
            visit.call(&n.caller_input)?;
            visit.call(&n.kwargs)?;
            visit.call(&n.value)?;
            for a in &n.anchors {
                visit.call(a)?;
            }
        }
        Ok(())
    }
    fn __clear__(&mut self, py: Python<'_>) {
        self.nodes.clear();
        self.aliases.clear();
        self.identities.bind(py).clear();
    }
}
