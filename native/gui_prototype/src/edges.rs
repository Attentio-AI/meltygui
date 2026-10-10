//! Retained, one-dimensional constraints. No renderer or platform dependencies.
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::collections::{BTreeMap, BTreeSet};

type Cell = (u64, u64, f64, Option<f64>);
#[derive(Clone)]
struct Edge {
    owner: u64,
    key: String,
    value: f64,
}
#[derive(Clone)]
struct Gesture {
    token: u64,
    edge: u64,
    version: u64,
    start: BTreeMap<u64, f64>,
}

#[pyclass(unsendable)]
#[derive(Clone)]
pub struct EdgeGraph {
    edges: BTreeMap<u64, Edge>,
    cells: BTreeMap<u64, Vec<Cell>>,
    next: u64,
    version: u64,
    gesture: Option<Gesture>,
    visits: usize,
}

impl EdgeGraph {
    fn snapshot(&self) -> BTreeMap<u64, f64> {
        self.edges.iter().map(|(&id, e)| (id, e.value)).collect()
    }
    fn restore(&mut self, values: &BTreeMap<u64, f64>) {
        for (&id, &value) in values {
            if let Some(e) = self.edges.get_mut(&id) {
                e.value = value;
            }
        }
    }
    fn merged(&self) -> Vec<Cell> {
        let mut cells: BTreeMap<(u64, u64), (f64, Option<f64>)> = BTreeMap::new();
        for &(a, b, min, max) in self.cells.values().flatten() {
            let entry = cells.entry((a, b)).or_insert((min, max));
            entry.0 = entry.0.max(min);
            entry.1 = match (entry.1, max) {
                (Some(x), Some(y)) => Some(x.min(y)),
                (x, None) => x,
                (None, y) => y,
            };
        }
        cells
            .into_iter()
            .map(|((a, b), (min, max))| (a, b, min, max))
            .collect()
    }
    fn feasible(&self) -> bool {
        // Difference constraints: b-a >= min, b-a <= max. A negative
        // cycle is contradictory even if no current drag happens to visit it.
        let cells = self.merged();
        let mut distance: BTreeMap<u64, f64> = self.edges.keys().map(|&id| (id, 0.)).collect();
        for iteration in 0..self.edges.len() {
            let mut changed = false;
            for &(a, b, min, max) in &cells {
                if distance[&a] > distance[&b] - min {
                    distance.insert(a, distance[&b] - min);
                    changed = true;
                }
                if let Some(cap) = max {
                    if distance[&b] > distance[&a] + cap {
                        distance.insert(b, distance[&a] + cap);
                        changed = true;
                    }
                }
            }
            if !changed {
                return true;
            }
            if iteration + 1 == self.edges.len() {
                return false;
            }
        }
        true
    }
    fn chain(
        cells: &[Cell],
        start: u64,
        goal: u64,
        walls: &BTreeSet<u64>,
        forward: bool,
        caps: bool,
        stack: &mut BTreeSet<u64>,
        memo: &mut BTreeMap<u64, Option<f64>>,
    ) -> Option<f64> {
        if start == goal {
            return Some(0.);
        }
        if walls.contains(&start) || stack.contains(&start) {
            return None;
        }
        if let Some(value) = memo.get(&start) {
            return *value;
        }
        stack.insert(start);
        let mut result: Option<f64> = None;
        for &(a, b, min, max) in cells {
            let (from, to) = if forward { (a, b) } else { (b, a) };
            if from != start {
                continue;
            }
            let weight = if caps {
                match max {
                    Some(v) => v,
                    None => continue,
                }
            } else {
                min
            };
            if let Some(rest) = Self::chain(cells, to, goal, walls, forward, caps, stack, memo) {
                let value = weight + rest;
                result = Some(match result {
                    None => value,
                    Some(old) => {
                        if caps {
                            old.min(value)
                        } else {
                            old.max(value)
                        }
                    }
                });
            }
        }
        stack.remove(&start);
        memo.insert(start, result);
        result
    }
    fn run(&mut self, edge: u64, mut target: f64, walls: &BTreeSet<u64>) -> PyResult<()> {
        let old = self.position(edge)?;
        if !target.is_finite() {
            return Err(PyValueError::new_err("non-finite edge target"));
        }
        if old == target || walls.contains(&edge) {
            return Ok(());
        }
        let forward = target > old;
        let cells = self.merged();
        for &(a, b, min, max) in &cells {
            if max.is_some_and(|v| v < min) {
                return Err(PyValueError::new_err(format!(
                    "contradictory span limits on {a}..{b}"
                )));
            }
        }
        for &wall in walls {
            let at = self.position(wall)?;
            if let Some(push) = Self::chain(
                &cells,
                edge,
                wall,
                walls,
                forward,
                false,
                &mut BTreeSet::new(),
                &mut BTreeMap::new(),
            ) {
                target = if forward {
                    target.min(at - push)
                } else {
                    target.max(at + push)
                };
            }
            if let Some(pull) = Self::chain(
                &cells,
                edge,
                wall,
                walls,
                !forward,
                true,
                &mut BTreeSet::new(),
                &mut BTreeMap::new(),
            ) {
                target = if forward {
                    target.min(at + pull)
                } else {
                    target.max(at - pull)
                };
            }
        }
        // A contradictory external size may put the old position beyond its
        // feasible range. Never move opposite to the requested direction here.
        target = if forward {
            target.max(old)
        } else {
            target.min(old)
        };
        self.edges.get_mut(&edge).unwrap().value = target;
        let mut pending = vec![edge];
        let mut remaining = 64 * (self.edges.len() + 1);
        while let Some(current) = pending.pop() {
            if remaining == 0 {
                return Err(PyRuntimeError::new_err(
                    "cyclic or unsatisfiable edge constraints",
                ));
            }
            remaining -= 1;
            self.visits += 1;
            let value = self.edges[&current].value;
            for &(a, b, min, max) in &cells {
                let candidate = if forward {
                    if a == current {
                        Some((b, value + min, true))
                    } else if b == current {
                        max.map(|cap| (a, value - cap, true))
                    } else {
                        None
                    }
                } else if b == current {
                    Some((a, value - min, false))
                } else if a == current {
                    max.map(|cap| (b, value + cap, false))
                } else {
                    None
                };
                if let Some((id, want, increase)) = candidate {
                    if walls.contains(&id) {
                        continue;
                    }
                    let item = self.edges.get_mut(&id).unwrap();
                    if (increase && item.value < want) || (!increase && item.value > want) {
                        item.value = want;
                        pending.push(id);
                    }
                }
            }
        }
        Ok(())
    }
    fn delta(&self, before: &BTreeMap<u64, f64>) -> Vec<(u64, f64)> {
        self.edges
            .iter()
            .filter_map(|(&id, e)| (before.get(&id) != Some(&e.value)).then_some((id, e.value)))
            .collect()
    }
}

#[pymethods]
impl EdgeGraph {
    #[new]
    fn new() -> Self {
        Self {
            edges: BTreeMap::new(),
            cells: BTreeMap::new(),
            next: 0,
            version: 0,
            gesture: None,
            visits: 0,
        }
    }

    fn fork(&self) -> Self {
        self.clone()
    }
    fn edge(&mut self, owner: u64, key: String, initial: f64) -> PyResult<u64> {
        if !initial.is_finite() {
            return Err(PyValueError::new_err("non-finite initial edge"));
        }
        if let Some((&id, _)) = self
            .edges
            .iter()
            .find(|(_, e)| e.owner == owner && e.key == key)
        {
            return Ok(id);
        }
        self.next += 1;
        self.version += 1;
        self.edges.insert(
            self.next,
            Edge {
                owner,
                key,
                value: initial,
            },
        );
        Ok(self.next)
    }
    fn position(&self, id: u64) -> PyResult<f64> {
        self.edges
            .get(&id)
            .map(|e| e.value)
            .ok_or_else(|| PyValueError::new_err(format!("retired or unknown edge {id}")))
    }
    fn values(&self) -> BTreeMap<u64, f64> {
        self.snapshot()
    }
    fn owner_edges(&self, owner: u64) -> Vec<u64> {
        self.edges
            .iter()
            .filter_map(|(&id, e)| (e.owner == owner).then_some(id))
            .collect()
    }
    fn set_positions(&mut self, values: BTreeMap<u64, f64>) -> PyResult<Vec<(u64, f64)>> {
        for (&id, &value) in &values {
            self.position(id)?;
            if !value.is_finite() {
                return Err(PyValueError::new_err("non-finite edge"));
            }
        }
        let before = self.snapshot();
        self.restore(&values);
        Ok(self.delta(&before))
    }
    fn replace_cells(&mut self, owner: u64, cells: Vec<Cell>) -> PyResult<bool> {
        for &(a, b, min, max) in &cells {
            self.position(a)?;
            self.position(b)?;
            if a == b
                || !min.is_finite()
                || min < 0.
                || max.is_some_and(|v| !v.is_finite() || v < min)
            {
                return Err(PyValueError::new_err(
                    "invalid cell: distinct edges and finite 0 <= minimum <= maximum required",
                ));
            }
        }
        if self.cells.get(&owner) == Some(&cells) {
            return Ok(false);
        }
        let old = self.cells.insert(owner, cells);
        if !self.feasible() {
            match old {
                Some(v) => {
                    self.cells.insert(owner, v);
                }
                None => {
                    self.cells.remove(&owner);
                }
            }
            return Err(PyValueError::new_err(
                "cyclic or contradictory span constraints",
            ));
        }
        self.version += 1;
        self.gesture = None;
        Ok(true)
    }
    fn remove(&mut self, owner: u64) -> Vec<u64> {
        let removed: BTreeSet<u64> = self
            .edges
            .iter()
            .filter_map(|(&id, e)| (e.owner == owner).then_some(id))
            .collect();
        self.cells.remove(&owner);
        // Registrations borrowing deleted edges retire with those edges.
        let dependents: Vec<u64> = self
            .cells
            .iter()
            .filter_map(|(&id, c)| {
                c.iter()
                    .any(|(a, b, _, _)| removed.contains(a) || removed.contains(b))
                    .then_some(id)
            })
            .collect();
        for id in &dependents {
            self.cells.remove(id);
        }
        for id in removed {
            self.edges.remove(&id);
        }
        self.version += 1;
        self.gesture = None;
        dependents
    }
    #[pyo3(signature=(edge, target, walls=Vec::new()))]
    fn solve(&mut self, edge: u64, target: f64, walls: Vec<u64>) -> PyResult<Vec<(u64, f64)>> {
        let before = self.snapshot();
        self.visits = 0;
        if let Err(e) = self.run(edge, target, &walls.into_iter().collect()) {
            self.restore(&before);
            return Err(e);
        }
        Ok(self.delta(&before))
    }
    #[pyo3(signature=(token, edge, displacement, walls=Vec::new(), opposite=None))]
    fn drag(
        &mut self,
        token: u64,
        edge: u64,
        displacement: f64,
        walls: Vec<u64>,
        opposite: Option<u64>,
    ) -> PyResult<Vec<(u64, f64)>> {
        if !displacement.is_finite() {
            return Err(PyValueError::new_err("non-finite displacement"));
        }
        self.position(edge)?;
        if let Some(other) = opposite {
            self.position(other)?;
        }
        if self.gesture.as_ref().map_or(true, |g| {
            g.token != token || g.edge != edge || g.version != self.version
        }) {
            self.gesture = Some(Gesture {
                token,
                edge,
                version: self.version,
                start: self.snapshot(),
            });
        }
        let mut start = self.gesture.as_ref().unwrap().start.clone();
        // Sticky replay restores movable geometry, not external boundaries.
        // Native acknowledgements can refine a display wall during a drag.
        for wall in &walls {
            start.insert(*wall, self.position(*wall)?);
        }
        let before = self.snapshot();
        self.restore(&start);
        self.visits = 0;
        let walls: BTreeSet<u64> = walls.into_iter().collect();
        let target = start[&edge] + displacement;
        let result = (|| {
            self.run(edge, target, &walls)?;
            if let Some(other) = opposite {
                let residual = target - self.position(edge)?;
                // Only expansion of the active cell can flip at a wall.
                if (start[&edge] - start[&other]) * displacement > 0. && residual.abs() > 1e-8 {
                    let mut held = walls.clone();
                    held.insert(edge);
                    self.run(other, self.position(other)? - residual, &held)?;
                }
            }
            Ok(())
        })();
        if let Err(e) = result {
            self.restore(&before);
            return Err(e);
        }
        Ok(self.delta(&before))
    }
    fn end_drag(&mut self) {
        self.gesture = None;
    }
    #[pyo3(signature=(near, far, maximum=false))]
    fn span(&self, near: u64, far: u64, maximum: bool) -> Option<f64> {
        Self::chain(
            &self.merged(),
            near,
            far,
            &BTreeSet::new(),
            true,
            maximum,
            &mut BTreeSet::new(),
            &mut BTreeMap::new(),
        )
    }
    fn stats(&self) -> (usize, usize, u64, usize) {
        (
            self.edges.len(),
            self.cells.values().map(Vec::len).sum(),
            self.version,
            self.visits,
        )
    }
}
