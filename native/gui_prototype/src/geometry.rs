//! Relative state slots, local visibility indexes and on-demand space conversion.
use crate::{NativeState, Value};
use pyo3::prelude::*;
use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet};
use std::rc::Rc;

pub type Rect = (f64, f64, f64, f64);
pub type Changes = Rc<RefCell<HashMap<u64, u8>>>;
pub const POSITION: u8 = 1;
pub const SIZE: u8 = 2;
pub const SCROLL: u8 = 4;
pub const CONTENT: u8 = 8;

pub fn effect(name: &str) -> u8 {
    match name {
        "left" | "top" | "content_offset" => POSITION,
        "width" | "height" => SIZE,
        "scroll_offset" => SCROLL,
        "unique" | "dlt_count" | "content_width" | "content_height" | "measured_width"
        | "abs_left" | "abs_top" | "cache" | "view_events" | "depth" => 0,
        _ => CONTENT,
    }
}

// Index memberships are derived acceleration data, never placement authority.
#[derive(Default)]
struct Index {
    cells: HashMap<(i64, i64), HashSet<u64>>,
    members: HashMap<u64, Vec<(i64, i64)>>,
    large: HashSet<u64>,
}
impl Index {
    fn remove(&mut self, id: u64) {
        if let Some(cells) = self.members.remove(&id) {
            for cell in cells {
                if let Some(ids) = self.cells.get_mut(&cell) {
                    ids.remove(&id);
                    if ids.is_empty() {
                        self.cells.remove(&cell);
                    }
                }
            }
        }
        self.large.remove(&id);
    }
    fn range(r: Rect) -> (i64, i64, i64, i64) {
        (
            (r.0 / 256.).floor() as i64,
            (r.1 / 256.).floor() as i64,
            ((r.0 + r.2) / 256.).floor() as i64,
            ((r.1 + r.3) / 256.).floor() as i64,
        )
    }
    fn insert(&mut self, id: u64, rect: Rect) {
        self.remove(id);
        let (x0, y0, x1, y1) = Self::range(rect);
        if (x1 - x0 + 1).saturating_mul(y1 - y0 + 1) > 256 {
            self.large.insert(id);
            return;
        }
        let mut cells = Vec::new();
        for x in x0..=x1 {
            for y in y0..=y1 {
                self.cells.entry((x, y)).or_default().insert(id);
                cells.push((x, y));
            }
        }
        self.members.insert(id, cells);
    }
    fn query(&self, rect: Rect) -> HashSet<u64> {
        let mut out = self.large.clone();
        let (x0, y0, x1, y1) = Self::range(rect);
        for x in x0..=x1 {
            for y in y0..=y1 {
                if let Some(ids) = self.cells.get(&(x, y)) {
                    out.extend(ids);
                }
            }
        }
        out
    }
}

struct Entry {
    state: Py<NativeState>,
    parent: u64,
    slots: [usize; 6],
    index: Index,
    extent: (f64, f64),
    stamp: Rc<Cell<u64>>,
}

#[derive(Default)]
pub struct Geometry {
    entries: HashMap<u64, Entry>,
    pub changes: Changes,
    roots: HashSet<u64>,
    origins: HashMap<u64, (u64, (f64, f64))>,
    pub candidates: u64,
    pub transforms: u64,
}
pub fn intersects(a: Rect, b: Rect) -> bool {
    a.0 < b.0 + b.2 && b.0 < a.0 + a.2 && a.1 < b.1 + b.3 && b.1 < a.1 + a.3
}
impl Geometry {
    pub fn attach(&mut self, py: Python<'_>, id: u64, parent: u64, state: Py<NativeState>) {
        let mut s = state.borrow_mut(py);
        let slots = [
            "left",
            "top",
            "width",
            "height",
            "scroll_offset",
            "content_offset",
        ]
        .map(|name| s.shape.borrow_mut().slot(name));
        s.observer = Some((id, Rc::downgrade(&self.changes)));
        drop(s);
        let stamp = self
            .entries
            .get(&parent)
            .map(|e| e.stamp.clone())
            .unwrap_or_default();
        self.entries.insert(
            id,
            Entry {
                state,
                parent,
                slots,
                index: Index::default(),
                extent: (0., 0.),
                stamp,
            },
        );
        if parent == 0 {
            self.roots.insert(id);
        }
        self.reindex(py, id);
    }
    pub fn state(&self, py: Python<'_>, id: u64) -> Py<NativeState> {
        self.entries[&id].state.clone_ref(py)
    }
    pub fn state_ref(&self, id: u64) -> &Py<NativeState> {
        &self.entries[&id].state
    }
    pub fn refresh(&mut self, py: Python<'_>, id: u64) {
        self.reindex(py, id);
    }
    pub fn rect(&self, py: Python<'_>, id: u64) -> Rect {
        let e = &self.entries[&id];
        let s = e.state.borrow(py);
        (
            s.at(e.slots[0]).and_then(Value::number).unwrap_or(0.),
            s.at(e.slots[1]).and_then(Value::number).unwrap_or(0.),
            s.at(e.slots[2]).and_then(Value::number).unwrap_or(0.),
            s.at(e.slots[3]).and_then(Value::number).unwrap_or(0.),
        )
    }
    pub fn scroll(&self, py: Python<'_>, id: u64) -> (f64, f64) {
        let e = &self.entries[&id];
        e.state
            .borrow(py)
            .at(e.slots[4])
            .and_then(Value::point)
            .unwrap_or((0., 0.))
    }
    pub fn offset(&self, py: Python<'_>, id: u64) -> (f64, f64) {
        let e = &self.entries[&id];
        e.state
            .borrow(py)
            .at(e.slots[5])
            .and_then(Value::point)
            .unwrap_or((0., 0.))
    }
    fn reindex(&mut self, py: Python<'_>, id: u64) {
        let rect = self.rect(py, id);
        let parent = self.entries[&id].parent;
        if let Some(p) = self.entries.get_mut(&parent) {
            p.index.insert(id, rect);
            p.extent.0 = p.extent.0.max(rect.0 + rect.2);
            p.extent.1 = p.extent.1.max(rect.1 + rect.3);
        }
        if let Some(e) = self.entries.get(&id) {
            e.stamp.set(e.stamp.get() + 1);
        }
    }
    pub fn sync(&mut self, py: Python<'_>) -> Vec<(u64, u8)> {
        let changes: Vec<_> = self.changes.borrow_mut().drain().collect();
        for (id, flags) in &changes {
            if !self.entries.contains_key(id) {
                continue;
            }
            if flags & (POSITION | SIZE) != 0 {
                self.reindex(py, *id);
            } else if flags & SCROLL != 0 {
                let e = &self.entries[id];
                e.stamp.set(e.stamp.get() + 1);
            }
        }
        changes
    }
    pub fn remove(&mut self, py: Python<'_>, id: u64) {
        if let Some(e) = self.entries.remove(&id) {
            e.stamp.set(e.stamp.get() + 1);
            e.state.borrow_mut(py).observer = None;
            if let Some(p) = self.entries.get_mut(&e.parent) {
                p.index.remove(id);
            }
        }
        self.roots.remove(&id);
        self.origins.remove(&id);
        self.changes.borrow_mut().remove(&id);
    }
    pub fn origin(&mut self, py: Python<'_>, id: u64) -> (f64, f64) {
        if id == 0 {
            return (0., 0.);
        }
        let current = self.entries[&id].stamp.get();
        if let Some((epoch, p)) = self.origins.get(&id) {
            if *epoch == current {
                return *p;
            }
        }
        let parent = self.entries[&id].parent;
        let mut p = self.origin(py, parent);
        if parent != 0 {
            let s = self.scroll(py, parent);
            p.0 -= s.0;
            p.1 -= s.1;
        }
        let r = self.rect(py, id);
        let offset = self.offset(py, id);
        p.0 += r.0 + offset.0;
        p.1 += r.1 + offset.1;
        self.origins.insert(id, (current, p));
        self.transforms += 1;
        p
    }
    pub fn children(&mut self, py: Python<'_>, id: u64, viewport: Rect) -> Vec<u64> {
        let ids = self.entries[&id].index.query(viewport);
        self.candidates += ids.len() as u64;
        let mut ids: Vec<_> = ids
            .into_iter()
            .filter(|c| intersects(self.rect(py, *c), viewport))
            .collect();
        ids.sort_unstable();
        ids
    }
    pub fn visible_children(&mut self, py: Python<'_>, id: u64) -> Vec<u64> {
        let r = self.rect(py, id);
        let s = self.scroll(py, id);
        self.children(py, id, (s.0, s.1, r.2, r.3))
    }
    pub fn roots(&self) -> Vec<u64> {
        let mut roots: Vec<_> = self.roots.iter().copied().collect();
        roots.sort_unstable();
        roots
    }
    pub fn extent(&mut self, py: Python<'_>, id: u64, children: &[u64]) -> (f64, f64) {
        let mut extent = (0_f64, 0_f64);
        for child in children {
            let r = self.rect(py, *child);
            extent.0 = extent.0.max(r.0 + r.2);
            extent.1 = extent.1.max(r.1 + r.3);
        }
        self.entries.get_mut(&id).unwrap().extent = extent;
        extent
    }
    pub fn scroll_limit(&self, py: Python<'_>, id: u64) -> (f64, f64) {
        let r = self.rect(py, id);
        let e = self.entries[&id].extent;
        ((e.0 - r.2).max(0.), (e.1 - r.3).max(0.))
    }
}
