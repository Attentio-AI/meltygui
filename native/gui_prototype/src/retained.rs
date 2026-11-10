//! Explicit invalidation and ownership, independent of rasterization.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyTuple};
use std::collections::BTreeSet;
use crate::hash::{Map as HashMap, Set as HashSet};
use crate::geometry::{Geometry, POSITION, SIZE, SCROLL, CONTENT};
use crate::{NativeState, Value};

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
    // Keep logical geometry at collision-solver precision: rounded captures
    // would otherwise feed changed spans back into shared layout edges.
    auto_height: bool,
    rendered_size: (f64,f64),
    depth: usize,
    scrollable: bool,
    in_scroll: bool,
    layout_changes: u64,
    z: u64,
    executions: u64,
    aliases: Vec<String>,
    anchors: Vec<Py<PyAny>>,
}

pub(crate) fn same_argument(a: &Bound<'_, PyAny>, b: &Bound<'_, PyAny>) -> PyResult<bool> {
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
    geometry: Geometry,
    dirty_set: HashSet<u64>,
    unclipped_dirty: HashSet<u64>,
    compose_set: HashSet<u64>,
    portals: BTreeSet<(u64,u64)>,
    placements: HashMap<u64, HashSet<u64>>,
    visibility_revision: u64,
}

impl RetainedGraph {
    /// `declare` plus its scheduling flags. `visibility` evaluates flag 8;
    /// a caller that already knows the view is clean has no use for it.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn declare_flags(&mut self, py: Python<'_>, parent: u64, renderer: u64, key: Py<PyAny>, input: Py<PyAny>,
                                kwargs: Py<PyDict>, rect: (f64, f64, f64, f64), portal: bool,
                                state: Option<Py<NativeState>>, scrollable: bool, visibility: bool) -> PyResult<(u64, u8)> {
        let id = self.declare(py, parent, renderer, key, input, kwargs, rect, portal, state, scrollable)?;
        let node = &self.nodes[&id];
        let mut flags = 0_u8;
        if node.pending {flags |= 1;}
        if node.dirty {flags |= 2;}
        if parent != 0 && self.nodes[&parent].scrollable {
            flags |= 4;
            if visibility && self.visible(py, id) {flags |= 8;}
        }
        Ok((id, flags))
    }
    pub(crate) fn is_visible(&mut self, py: Python<'_>, id: u64) -> bool { self.visible(py, id) }
    /// Whether `id` is a live child of `parent` with nothing to execute or return.
    pub(crate) fn clean_child(&self, id: u64, parent: u64) -> bool {
        self.nodes.get(&id).is_some_and(|node| node.parent == parent && !node.dirty && !node.pending && !node.portal)
    }
    /// The consumed result and current extent a declaration returns to its caller.
    pub(crate) fn conclude(&mut self, py: Python<'_>, id: u64) -> PyResult<(bool, Py<PyAny>, f64, f64)> {
        let rect = self.rect(py, id);
        let (pending, value) = self.result(py, id, true)?;
        Ok((pending, value, rect.2, rect.3))
    }
    pub(crate) fn constraints(&mut self,py:Python<'_>,id:u64)->(f64,f64) {
        self.sync(py);let r=self.geometry.rect(py,id);
        (r.2,if self.nodes[&id].auto_height {-1.}else{r.3})
    }
    fn composition(&mut self, mut id: u64) {
        while let Some(node) = self.nodes.get_mut(&id) {
            node.compose = true;
            if node.executions>0 {self.compose_set.insert(id);}
            if node.portal {
                break;
            }
            id = node.parent;
        }
    }
    fn remove_node(&mut self, py: Python<'_>, id: u64) -> PyResult<()> {
        if let Some(n) = self.nodes.remove(&id) {
            self.visibility_revision += 1;
            self.placements.remove(&id);
            if let Some(children)=self.placements.get_mut(&n.parent) {children.remove(&id);}
            self.geometry.remove(py,id);
            self.dirty_set.remove(&id);self.unclipped_dirty.remove(&id);self.compose_set.remove(&id);self.portals.remove(&(n.z,id));
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
    fn depth(&self, id:u64) -> usize { self.nodes.get(&id).map_or(0,|n|n.depth) }
    fn sync(&mut self, py: Python<'_>) {
        for (id, flags) in self.geometry.sync(py) {
            if !self.nodes.contains_key(&id) {continue;}
            if flags != 0 {self.visibility_revision += 1;}
            let parent=self.nodes[&id].parent;
            if parent!=0 && !self.nodes[&id].portal && flags & (POSITION|SIZE)!=0 {
                self.placements.entry(parent).or_default().insert(id);
            }
            if flags & (SIZE|CONTENT)!=0 && self.nodes[&id].declared.is_none() {self.invalidate_node(id);}
            if flags & (SIZE|SCROLL|CONTENT)!=0 {self.composition(id);}
            if flags & POSITION!=0 && !self.nodes[&id].portal {self.composition(parent);}
        }
    }
    fn collect_visible(&mut self, py: Python<'_>, id:u64, out:&mut Vec<u64>) {
        out.push(id);
        for child in self.geometry.visible_children(py,id) {
            if !self.nodes[&child].portal {self.collect_visible(py,child,out);}
        }
    }
    fn hit_branch(&mut self, py: Python<'_>, id:u64, x:f64,y:f64, origin:(f64,f64), out:&mut Vec<(u64,f64,f64)>) {
        let r=self.geometry.rect(py,id);
        if x<origin.0 || y<origin.1 || x>=origin.0+r.2 || y>=origin.1+r.3 {return;}
        let scroll=self.geometry.scroll(py,id);
        let local=(x-origin.0+scroll.0,y-origin.1+scroll.1);
        for child in self.geometry.children(py,id,(local.0,local.1,0.000001,0.000001)).into_iter().rev() {
            if self.nodes[&child].portal {continue;}
            let c=self.geometry.rect(py,child);let o=self.geometry.offset(py,child);
            self.hit_branch(py,child,x,y,(origin.0-scroll.0+c.0+o.0,origin.1-scroll.1+c.1+o.1),out);
        }
        out.push((id,origin.0,origin.1));
    }
}

#[pymethods]
impl RetainedGraph {
    #[new]
    fn new(py: Python<'_>) -> Self {
        Self {
            identities: PyDict::new(py).unbind(),
            nodes: HashMap::default(),
            serial: 0,
            rank: 0,
            aliases: HashMap::default(),
            retired: vec![],
            geometry: Geometry::default(),
            dirty_set: HashSet::default(),
            unclipped_dirty: HashSet::default(),
            compose_set: HashSet::default(),
            portals: BTreeSet::new(),
            placements: HashMap::default(),
            visibility_revision: 0,
        }
    }
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (parent, renderer, key, input, kwargs, rect, portal, state=None, scrollable=false))]
    fn declare(
        &mut self,
        py: Python<'_>,
        parent: u64,
        renderer: u64,
        key: Py<PyAny>,
        input: Py<PyAny>,
        kwargs: Py<PyDict>,
        rect: (f64, f64, f64, f64),
        portal: bool,
        state: Option<Py<NativeState>>,
        scrollable: bool,
    ) -> PyResult<u64> {
        self.sync(py);
        let supplied_state=state.is_some();
        let in_scroll=scrollable || self.nodes.get(&parent).is_some_and(|n|n.in_scroll);
        if parent != 0 && !self.nodes.contains_key(&parent) {
            return Err(PyRuntimeError::new_err("missing owner"));
        }
        let identity = (parent, renderer, key).into_pyobject(py)?.into_any();
        let id = if let Some(id) = self.identities.bind(py).get_item(&identity)? {
            id.extract()?
        } else {
            self.serial += 1;
            self.visibility_revision += 1;
            self.rank += 1;
            let id = self.serial;
            self.identities.bind(py).set_item(&identity, id)?;
            let state=match state {Some(s)=>s,None=>Py::new(py,NativeState::py_new())?};
            {
                let mut state=state.borrow_mut(py);
                if !supplied_state {
                    state.put("left",Value::Float(rect.0));state.put("top",Value::Float(rect.1));
                    state.put("width",Value::Float(rect.2));state.put("height",Value::Float(rect.3.max(0.)));
                }
                if state.lookup("scroll_offset").is_none() {state.put("scroll_offset",Value::Point(0.,0.));}
                if state.lookup("content_offset").is_none() {state.put("content_offset",Value::Point(0.,if portal {28.}else{0.}));}
            }
            self.geometry.attach(py,id,parent,state);
            self.dirty_set.insert(id);
            if !in_scroll {self.unclipped_dirty.insert(id);}
            if portal {self.portals.insert((self.rank,id));}
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
                    auto_height: rect.3<0.,
                    rendered_size: (rect.2,rect.3.max(0.)),
                    depth: self.depth(parent)+1,
                    scrollable,
                    in_scroll,
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
        if node.portal != portal || node.scrollable != scrollable || node.auto_height != (rect.3<0.) {
            self.visibility_revision += 1;
            node.dirty = true;self.dirty_set.insert(id);if !in_scroll {self.unclipped_dirty.insert(id);}
        }
        if node.portal != portal {
            if portal {self.portals.insert((node.z,id));} else {self.portals.remove(&(node.z,id));}
        }
        node.auto_height = rect.3<0.;
        node.portal = portal;node.scrollable=scrollable;node.in_scroll=in_scroll;
        if !supplied_state {
            let state=self.geometry.state(py,id);let mut state=state.borrow_mut(py);
            if !portal {state.put("left",Value::Float(rect.0));state.put("top",Value::Float(rect.1));}
            state.put("width",Value::Float(rect.2));
            if rect.3>=0. {state.put("height",Value::Float(rect.3));}
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
    /// `declare` plus the scheduling facts its caller needs, in one crossing.
    /// Flags: 1 pending returned edit, 2 dirty, 4 scrollable parent, 8 visible
    /// (only evaluated under a scrollable parent, where it decides culling).
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (parent, renderer, key, input, kwargs, rect, portal, state=None, scrollable=false))]
    fn declare_view(
        &mut self,
        py: Python<'_>,
        parent: u64,
        renderer: u64,
        key: Py<PyAny>,
        input: Py<PyAny>,
        kwargs: Py<PyDict>,
        rect: (f64, f64, f64, f64),
        portal: bool,
        state: Option<Py<NativeState>>,
        scrollable: bool,
    ) -> PyResult<(u64, u8)> {
        self.declare_flags(py, parent, renderer, key, input, kwargs, rect, portal, state, scrollable, true)
    }
    /// A consumed result plus the current extent; see `conclude`.
    #[pyo3(name = "conclude")]
    fn py_conclude(&mut self, py: Python<'_>, id: u64) -> PyResult<(bool, Py<PyAny>, f64, f64)> {
        self.conclude(py, id)
    }
    #[pyo3(name = "constraints")]
    fn py_constraints(&mut self, py: Python<'_>, id: u64) -> (f64, f64) {
        self.constraints(py, id)
    }
    /// Start one execution. Returns the live input, the declaration's
    /// arguments (shared, not copied: read only), the pre-execution rectangle,
    /// origin, parent, portal flag, automatic-height flag and scroll offset.
    /// `invocation` materializes the same data as one keyword dictionary.
    #[allow(clippy::type_complexity)]
    fn begin_execution(&mut self, py: Python<'_>, id: u64)
        -> PyResult<(Py<PyAny>, Py<PyDict>, (f64,f64,f64,f64), (f64,f64), u64, bool, bool, (f64,f64))> {
        let node = self.nodes.get(&id).ok_or_else(|| PyRuntimeError::new_err("retired view"))?;
        let (input, args) = (node.input.clone_ref(py), node.kwargs.clone_ref(py));
        let rect = self.rect(py, id);
        self.begin(py, id)?;
        let origin = self.geometry.origin(py, id);
        let scroll = self.geometry.scroll(py, id);
        let node = &self.nodes[&id];
        Ok((input, args, rect, origin, node.parent, node.portal, node.auto_height, scroll))
    }
    /// Commit one execution and its measured extent. Returns whether the extent
    /// changed, whether nodes await retirement, the parent, and the capture's
    /// residency owner (the ancestor directly below a scroll viewport, or 0).
    fn finish_execution(&mut self, py: Python<'_>, id: u64, result: &Bound<'_, PyTuple>, independent: bool,
                        width: f64, height: f64) -> PyResult<(bool, bool, u64, u64)> {
        self.commit(py, id, result, independent)?;
        let resized = self.measured(py, id, width, height)?;
        let (parent, owner) = self.capture_owner(id);
        Ok((resized, !self.retired.is_empty(), parent, owner))
    }
    /// A capture's parent and its residency owner: the ancestor directly below
    /// the nearest scroll viewport, or 0 when no viewport contains it.
    fn capture_owner(&self, id: u64) -> (u64, u64) {
        let parent = self.nodes[&id].parent;
        let (mut owner, mut ancestor) = (id, parent);
        while ancestor != 0 && !self.nodes[&ancestor].scrollable {
            owner = ancestor;
            ancestor = self.nodes[&ancestor].parent;
        }
        (parent, if ancestor != 0 {owner} else {0})
    }
    /// What automatic raster selection needs for one observed view: whether a
    /// rasterized ancestor already covers it, its pixel count and whether it is
    /// a raster now. None for roots and portals, which are always rasters.
    fn raster_context(&mut self, py: Python<'_>, gpu: &Bound<'_, crate::texture::TextureCache>, id: u64)
        -> Option<(bool, i64, bool)> {
        let node = self.nodes.get(&id)?;
        if node.parent == 0 || node.portal {return None;}
        let cache = gpu.borrow();
        let (mut ancestor, mut covered) = (node.parent, false);
        while ancestor != 0 {
            let owner = &self.nodes[&ancestor];
            if owner.portal {break;}
            if owner.parent != 0 && cache.is_raster(ancestor) {covered = true; break;}
            ancestor = owner.parent;
        }
        let raster = cache.is_raster(id);
        drop(cache);
        let rect = self.rect(py, id);
        Some((covered, (rect.2.ceil() as i64) * (rect.3.ceil() as i64), raster))
    }

    /// Replay every pending composition bottom-up in one crossing. `stack`
    /// holds views still executing, whose packets are incomplete. Returns the
    /// visible scroll children, for the caller's residency ordering.
    fn compose(&mut self, py: Python<'_>, gpu: &Bound<'_, crate::texture::TextureCache>,
               backgrounds: &Bound<'_, PyDict>, stack: Vec<u64>) -> PyResult<Vec<u64>> {
        let nodes: Vec<u64> = self.composition_order(py).into_iter().filter(|id| !stack.contains(id)).collect();
        let mut touched = vec![];
        if nodes.is_empty() {return Ok(touched);}
        let mut cache = gpu.borrow_mut();
        for node in &nodes {
            let placements = self.take_placements(py, *node);
            if self.nodes[node].scrollable {
                let children = self.visible_children(py, *node);
                touched.extend(children.iter().map(|child| child.0));
                cache.children(*node, children.into_iter()
                    .map(|(child,x,y,w,h)| (child, x as f32, y as f32, w as f32, h as f32)).collect());
            } else {
                for (child,x,y,w,h) in placements {
                    if backgrounds.contains(child)? {
                        cache.place_child(*node, child, (x as f32, y as f32, w as f32, h as f32),
                                          (x as f32, y as f32, (x+w) as f32, (y+h) as f32))?;
                    }
                }
            }
        }
        let mut views = Vec::with_capacity(nodes.len());
        for node in &nodes {
            let background = backgrounds.get_item(*node)?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(*node))?;
            views.push((*node, background.extract::<(f32,f32,f32,f32)>()?));
        }
        cache.render_many(views)?;
        drop(cache);
        for node in &nodes {self.composed(*node);}
        Ok(touched)
    }
    fn invocation(&mut self, py: Python<'_>, id: u64) -> PyResult<(Py<PyAny>, Py<PyDict>)> {
        let n = self
            .nodes
            .get(&id)
            .ok_or_else(|| PyRuntimeError::new_err("retired view"))?;
        let args=n.kwargs.bind(py).copy()?;
        let r=self.geometry.rect(py,id);
        args.set_item(intern!(py,"width"),r.2)?;
        if n.auto_height {args.set_item(intern!(py,"height"),py.None())?;} else {args.set_item(intern!(py,"height"),r.3)?;}
        args.set_item(intern!(py,"left"),r.0)?;args.set_item(intern!(py,"top"),r.1)?;
        args.set_item(intern!(py,"scroll_offset"),self.geometry.scroll(py,id))?;
        Ok((n.input.clone_ref(py),args.unbind()))
    }
    fn begin(&mut self, py:Python<'_>, id: u64) -> PyResult<()> {
        let n = self
            .nodes
            .get_mut(&id)
            .ok_or_else(|| PyRuntimeError::new_err("retired view"))?;
        if n.declared.is_some() {
            return Err(PyRuntimeError::new_err("recursive retained-node execution"));
        }
        n.declared = Some(vec![]);
        let rect=self.geometry.rect(py,id);
        n.rendered_size=(rect.2,rect.3);
        n.dirty = false;
        self.dirty_set.remove(&id);self.unclipped_dirty.remove(&id);
        self.geometry.state(py,id).borrow_mut(py).rendering=true;
        Ok(())
    }
    fn abort(&mut self, py: Python<'_>, id: u64) -> PyResult<()> {
        if let Some(n) = self.nodes.get_mut(&id) {
            n.dirty = true;self.dirty_set.insert(id);if !n.in_scroll {self.unclipped_dirty.insert(id);}
            self.geometry.state(py,id).borrow_mut(py).rendering=false;
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
        self.geometry.state(py,id).borrow_mut(py).rendering=false;
        self.geometry.extent(py,id,&n.children);
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
                self.dirty_set.insert(parent);if !p.in_scroll {self.unclipped_dirty.insert(parent);}
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
    fn measured(&mut self, py:Python<'_>, id:u64, width:f64,height:f64)->PyResult<bool> {
        if !width.is_finite() || !height.is_finite() || width<=0. || height<0. {return Err(PyRuntimeError::new_err("invalid measured extent"));}
        let n=self.nodes.get_mut(&id).ok_or_else(||PyRuntimeError::new_err("retired view"))?;
        let changed=n.rendered_size!=(width,height);
        n.rendered_size=(width,height);
        let parent=n.parent;let portal=n.portal;
        let state=self.geometry.state(py,id);let mut state=state.borrow_mut(py);
        state.put("width",Value::Float(width));state.put("height",Value::Float(height));drop(state);
        if let Some(flags)=self.geometry.changes.borrow_mut().get_mut(&id) {*flags &= !SIZE;}
        if changed {
            self.visibility_revision += 1;
            n.layout_changes+=1;self.geometry.refresh(py,id);self.composition(id);
            if !portal {
                if let Some(owner)=self.nodes.get_mut(&parent) {
                    if owner.declared.is_none() && !owner.scrollable {owner.dirty=true;self.dirty_set.insert(parent);if !owner.in_scroll {self.unclipped_dirty.insert(parent);}}
                }
            }
        }
        Ok(changed)
    }
    fn pending(&self, id: u64) -> bool {
        self.nodes.get(&id).is_some_and(|n| n.pending)
    }
    fn in_scroll(&self, id:u64)->bool {self.nodes.get(&id).is_some_and(|n|n.in_scroll)}
    fn invalidate_node(&mut self, id: u64) {
        if let Some(n) = self.nodes.get_mut(&id) {
            n.dirty = true;self.dirty_set.insert(id);if !n.in_scroll {self.unclipped_dirty.insert(id);}
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
    fn dirty_nodes(&mut self,py:Python<'_>) -> Vec<u64> {
        self.sync(py);
        if self.dirty_set.is_empty() {return vec![];}
        let mut visible=vec![];
        for id in self.geometry.roots() {if !self.nodes[&id].portal {self.collect_visible(py,id,&mut visible);}}
        for id in self.windows() {self.collect_visible(py,id,&mut visible);}
        visible.retain(|id|self.dirty_set.contains(id));
        visible.extend(self.unclipped_dirty.iter().copied());
        visible.sort_unstable();visible.dedup();
        visible.sort_by_key(|id|(self.depth(*id),*id));visible
    }
    fn composition_order(&mut self,py:Python<'_>) -> Vec<u64> {
        self.sync(py);
        let mut ids:Vec<_>=self.compose_set.iter().copied().filter(|id| self.nodes[id].executions>0).collect();
        ids.sort_by_key(|id|(std::cmp::Reverse(self.depth(*id)),*id));ids
    }
    fn request_composition(&mut self, id: u64) { self.composition(id); }
    fn composed(&mut self, id: u64) {
        if let Some(n) = self.nodes.get_mut(&id) {
            n.compose = false;self.compose_set.remove(&id);
        }
    }
    fn take_placements(&mut self,py:Python<'_>,parent:u64)->Vec<(u64,f64,f64,f64,f64)> {
        self.sync(py);
        self.placements.remove(&parent).unwrap_or_default().into_iter()
            .filter(|id|self.nodes.contains_key(id)).map(|id|{
                let r=self.geometry.rect(py,id);
                (id,r.0,r.1,r.2,r.3)
            }).collect()
    }
    fn state(&self,py:Python<'_>,id:u64) -> Py<NativeState> {self.geometry.state(py,id)}
    #[pyo3(signature = (id, **parameters))]
    fn set_parameters(&mut self,py:Python<'_>,id:u64,parameters:Option<&Bound<'_,PyDict>>)->PyResult<()> {
        if !self.nodes.contains_key(&id) {return Err(PyRuntimeError::new_err("retired view"));}
        if let Some(parameters)=parameters {
            let state=self.geometry.state(py,id);let mut state=state.borrow_mut(py);
            for (name,value) in parameters.iter() {state.__setattr__(&name.extract::<String>()?,&value)?;}
        }
        self.sync(py);Ok(())
    }
    #[pyo3(signature = (id, relative_to=0, content=false))]
    fn origin(&mut self,py:Python<'_>,id:u64,relative_to:u64,content:bool)->PyResult<(f64,f64)> {
        if !self.nodes.contains_key(&id) || (relative_to!=0 && !self.nodes.contains_key(&relative_to)) {return Err(PyRuntimeError::new_err("unknown coordinate space"));}
        self.sync(py);
        let a=self.geometry.origin(py,id);let mut b=self.geometry.origin(py,relative_to);
        if content && relative_to!=0 {let s=self.geometry.scroll(py,relative_to);b.0-=s.0;b.1-=s.1;}
        Ok((a.0-b.0,a.1-b.1))
    }
    fn visible_children(&mut self,py:Python<'_>,id:u64)->Vec<(u64,f64,f64,f64,f64)> {
        self.sync(py);let scroll=self.geometry.scroll(py,id);
        self.geometry.visible_children(py,id).into_iter().filter(|c|!self.nodes[c].portal).map(|c|{
            let r=self.geometry.rect(py,c);(c,r.0-scroll.0,r.1-scroll.1,r.2,r.3)
        }).collect()
    }
    /// Changes to geometry, topology or portal protection invalidate eviction eligibility.
    fn visibility_revision(&mut self,py:Python<'_>)->u64 {
        self.sync(py);
        self.visibility_revision
    }
    fn visible(&mut self,py:Python<'_>,id:u64)->bool {
        self.sync(py);let p=self.geometry.origin(py,id);let r=self.geometry.rect(py,id);
        let mut clip=(p.0,p.1,p.0+r.2,p.1+r.3);
        let mut current=id;
        while let Some(n)=self.nodes.get(&current) {
            if n.portal || n.parent==0 {break;}
            current=n.parent;let p=self.geometry.origin(py,current);let r=self.geometry.rect(py,current);
            clip=(clip.0.max(p.0),clip.1.max(p.1),clip.2.min(p.0+r.2),clip.3.min(p.1+r.3));
        }
        clip.2>clip.0 && clip.3>clip.1
    }
    fn scroll_at(&mut self,py:Python<'_>,x:f64,y:f64,dx:f64,dy:f64)->bool {
        let (_,hits)=self.hits(py,x,y);
        for (id,_,_) in hits {
            if !self.nodes[&id].scrollable {continue;}
            let old=self.geometry.scroll(py,id);let max=self.geometry.scroll_limit(py,id);
            let next=((old.0+dx).clamp(0.,max.0),(old.1+dy).clamp(0.,max.1));
            if old!=next {
                let state=self.geometry.state(py,id);let mut state=state.borrow_mut(py);
                let slot=state.shape.borrow_mut().slot("scroll_offset");
                state.put_slot(slot,Value::Point(next.0,next.1));state.overrides.insert(slot,Value::Point(next.0,next.1));
                drop(state);self.sync(py);return true;
            }
        }
        false
    }
    fn geometry_stats(&self)->(u64,u64) {(self.geometry.candidates,self.geometry.transforms)}
    #[pyo3(signature = (point, source=0, target=0, source_content=false, target_content=false))]
    fn convert_point(&mut self,py:Python<'_>,point:(f64,f64),source:u64,target:u64,source_content:bool,target_content:bool)->PyResult<(f64,f64)> {
        if (source!=0 && !self.nodes.contains_key(&source)) || (target!=0 && !self.nodes.contains_key(&target)) {return Err(PyRuntimeError::new_err("unknown coordinate space"));}
        self.sync(py);
        let mut a=self.geometry.origin(py,source);let mut b=self.geometry.origin(py,target);
        if source_content && source!=0 {let s=self.geometry.scroll(py,source);a.0-=s.0;a.1-=s.1;}
        if target_content && target!=0 {let s=self.geometry.scroll(py,target);b.0-=s.0;b.1-=s.1;}
        Ok((point.0+a.0-b.0,point.1+a.1-b.1))
    }
    fn allocate(&mut self,py:Python<'_>,id:u64,rect:(f64,f64,f64,f64))->PyResult<bool> {
        if ![rect.0,rect.1,rect.2,rect.3].iter().all(|v|v.is_finite()) || rect.2<=0. || rect.3<0. {return Err(PyRuntimeError::new_err("invalid layout allocation"));}
        if !self.nodes.contains_key(&id) {return Err(PyRuntimeError::new_err("retired layout child"));}
        if self.geometry.rect(py,id)==rect {return Ok(false);}
        let s=self.geometry.state(py,id);let mut s=s.borrow_mut(py);
        for (name,value) in [("left",Value::Float(rect.0)),("top",Value::Float(rect.1)),("width",Value::Float(rect.2)),("height",Value::Float(rect.3))] {
            let python=value.to_py(py);
            s.__setattr__(name,python.bind(py))?;
        }
        self.nodes.get_mut(&id).unwrap().auto_height=false;
        drop(s);self.sync(py);Ok(true)
    }
    fn raise_window(&mut self, id: u64) {
        self.rank += 1;
        if let Some(n) = self.nodes.get_mut(&id) {
            self.visibility_revision += 1;
            self.portals.remove(&(n.z,id));n.z = self.rank;self.portals.insert((n.z,id));
        }
    }
    fn info(&mut self, py: Python<'_>, id: u64) -> PyResult<Py<PyDict>> {
        self.sync(py);
        let n = self.nodes.get(&id).ok_or_else(|| PyRuntimeError::new_err("retired view"))?;
        let d = PyDict::new(py);
        d.set_item("parent", n.parent)?;
        let r=self.geometry.rect(py,id);
        d.set_item("rect",r)?;
        d.set_item("scrollable",n.scrollable)?;
        d.set_item("portal", n.portal)?;
        d.set_item("executions", n.executions)?;
        d.set_item("constraints",(r.2,if n.auto_height {-1.}else{r.3}))?;
        d.set_item("layout_changes", n.layout_changes)?;
        d.set_item("children", &n.children)?;
        d.set_item("z", n.z)?;
        Ok(d.unbind())
    }
    fn contains(&self, id: u64) -> bool {
        self.nodes.contains_key(&id)
    }
    fn rect(&mut self,py:Python<'_>,id:u64)->(f64,f64,f64,f64) {self.sync(py);self.geometry.rect(py,id)}
    fn parent(&self,id:u64)->u64 {self.nodes[&id].parent}
    fn is_portal(&self,id:u64)->bool {self.nodes[&id].portal}
    fn is_scrollable(&self,id:u64)->bool {self.nodes[&id].scrollable}
    fn has_children(&self,id:u64)->bool {!self.nodes[&id].children.is_empty()}
    fn portal_owner(&self,mut id:u64)->Option<u64> {
        while let Some(n)=self.nodes.get(&id) {if n.portal {return Some(id);} id=n.parent;}
        None
    }
    fn visible_nodes(&mut self,py:Python<'_>)->Vec<u64> {
        self.sync(py);let mut out=vec![];
        for id in self.geometry.roots() {if !self.nodes[&id].portal {self.collect_visible(py,id,&mut out);}}
        for id in self.windows() {self.collect_visible(py,id,&mut out);}
        out
    }
    fn clip_rect(&mut self,py:Python<'_>,id:u64)->(f64,f64,f64,f64) {
        self.sync(py);let p=self.geometry.origin(py,id);let r=self.geometry.rect(py,id);
        let mut clip=(p.0,p.1,p.0+r.2,p.1+r.3);let mut current=id;
        while let Some(n)=self.nodes.get(&current) {
            if n.portal || n.parent==0 {break;}
            current=n.parent;let p=self.geometry.origin(py,current);let r=self.geometry.rect(py,current);
            clip=(clip.0.max(p.0),clip.1.max(p.1),clip.2.min(p.0+r.2),clip.3.min(p.1+r.3));
        }
        clip
    }

    /// One native query for ImGui and retained regions. Portals escape ancestor
    /// clips and occlude lower windows even when their body has no hit region.
    fn hits(&mut self,py:Python<'_>,x:f64,y:f64)->(Option<u64>,Vec<(u64,f64,f64)>) {
        self.sync(py);
        let mut portal=None;
        for id in self.windows().into_iter().rev() {
            let p=self.geometry.origin(py,id);let r=self.geometry.rect(py,id);let o=self.geometry.offset(py,id);
            if p.0<=x && x<p.0+r.2 && p.1-o.1<=y && y<p.1+r.3 {portal=Some(id);break;}
        }
        let mut hits=vec![];
        if let Some(id)=portal {let p=self.geometry.origin(py,id);self.hit_branch(py,id,x,y,p,&mut hits);}
        else {for id in self.geometry.roots().into_iter().rev() {
            if !self.nodes[&id].portal {let p=self.geometry.origin(py,id);self.hit_branch(py,id,x,y,p,&mut hits);}
        }}
        (portal,hits)
    }
    fn nodes(&self) -> Vec<u64> {
        let mut ids: Vec<_> = self.nodes.keys().copied().collect();
        ids.sort();
        ids
    }
    fn windows(&self)->Vec<u64> {self.portals.iter().map(|(_,id)|*id).collect()}
    fn take_retired(&mut self) -> Vec<u64> {
        std::mem::take(&mut self.retired)
    }
    fn retire(&mut self, py: Python<'_>, id: u64) -> PyResult<()> {
        self.remove_node(py, id)
    }
    fn clear(&mut self, py: Python<'_>) {
        if !self.nodes.is_empty() {self.visibility_revision += 1;}
        self.placements.clear();
        self.retired.extend(self.nodes.keys().copied());
        for id in self.nodes.keys() {self.geometry.remove(py,*id);}
        self.dirty_set.clear();self.unclipped_dirty.clear();self.compose_set.clear();self.portals.clear();
        self.nodes.clear();
        self.aliases.clear();
        self.identities = PyDict::new(py).unbind();
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.identities)?;
        for (id,n) in &self.nodes {
            visit.call(self.geometry.state_ref(*id))?;
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
        self.placements.clear();
        self.geometry=Geometry::default();self.dirty_set.clear();self.unclipped_dirty.clear();self.compose_set.clear();self.portals.clear();
        self.nodes.clear();
        self.aliases.clear();
        self.identities.bind(py).clear();
    }
}
