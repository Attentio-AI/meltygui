//! Document lines to displayed rows, and the folds that hide lines: what an
//! editor session keeps of its text's fold ranges.
//!
//! `Rows` holds two layers of foldable ranges (the scopes a `Source` read
//! from the text, and ranges its caller gives, such as unchanged diff
//! context), which of them are collapsed, and the one mapping between lines
//! and rows that all of them make. An edit is followed in place (`follow`):
//! ranges below it move, ranges around it stretch. Fresh scope ranges arrive
//! whole from the source's thread (`land`), the collapsed ones already found
//! there, so neither costs the session anything per range. Scope folds are
//! collapsed by name (`Facts::name`): that survives edits and restarts.
//! Whatever changes many folds at once does it in one call and one layout
//! (`set`, `fold_all`, `land`): a layout per fold is quadratic.
//! `Hits` keeps a search's hits on their lines the same way.
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::PyList;

use crate::facts::{nest, Facts, Keyed, Name, COMMENT, IMPORTS, STR};
use crate::Source;

/// Sorted line numbers that follow edits. An edit moves every line below
/// it, so the entries from `gap` on share one pending `shift`: an edit next
/// to the previous one costs nothing, one elsewhere moves the gap there.
#[derive(Default, Clone)]
struct Lines {
    at: Vec<u32>,
    gap: usize,
    shift: i64,
}

impl Lines {
    fn new(at: Vec<u32>) -> Lines {
        Lines { gap: at.len(), at, shift: 0 }
    }

    fn line(&self, index: usize) -> u32 {
        if index >= self.gap { self.at[index].wrapping_add(self.shift as u32) } else { self.at[index] }
    }

    /// Index of the first entry at or below `line`.
    fn find(&self, line: u32) -> usize {
        if self.gap > 0 && self.at[self.gap - 1] >= line {
            return self.at[..self.gap].partition_point(|&entry| entry < line);
        }
        // (A stored entry may be below zero, wrapped: only its sum with the shift is a line.)
        self.gap + self.at[self.gap..].partition_point(|&entry| entry.wrapping_add(self.shift as u32) < line)
    }

    /// The entries from `index` on are `delta` lines lower.
    fn shift(&mut self, index: usize, delta: i64) {
        if delta == 0 { return; }
        if self.shift != 0 {
            let (low, high, by) = if index > self.gap { (self.gap, index, self.shift) } else { (index, self.gap, -self.shift) };
            for entry in &mut self.at[low..high] { *entry = (*entry as i64 + by) as u32; }
        }
        self.gap = index;
        self.shift += delta;
    }

    /// Set one entry's line; the caller keeps the order.
    fn put(&mut self, index: usize, line: u32) {
        self.at[index] = if index >= self.gap { (line as i64 - self.shift) as u32 } else { line };
    }

    fn cut(&mut self, low: usize, high: usize) {
        self.at.drain(low..high);
        if self.gap >= high { self.gap -= high - low; } else if self.gap > low { self.gap = low; }
    }
}

/// One layer of foldable ranges: range `at` is lines `heads.line(at) ..= + spans[at]`.
#[derive(Default)]
struct Layer {
    heads: Lines,
    spans: Vec<u32>,
    /// The range around each one, or -1.
    parents: Vec<i32>,
    /// Ranges whose first line was deleted, sorted: gone until the next reading.
    dead: Vec<u32>,
    /// The collapsed ranges, sorted.
    closed: Vec<u32>,
}

impl Layer {
    fn alive(&self, at: usize) -> bool { self.dead.binary_search(&(at as u32)).is_err() }

    fn shut(&self, at: usize) -> bool { self.closed.binary_search(&(at as u32)).is_ok() }

    fn range(&self, at: usize) -> (u32, u32) {
        let head = self.heads.line(at);
        (head, head + self.spans[at])
    }

    /// Index of the outermost range headed by `line`.
    fn at(&self, line: u32) -> Option<usize> {
        (self.heads.find(line)..self.spans.len()).take_while(|&at| self.heads.line(at) == line).find(|&at| self.alive(at))
    }

    /// Indexes of the ranges holding `line`, innermost first.
    fn around(&self, line: u32) -> Vec<usize> {
        let mut found = vec![];
        let mut at = self.heads.find(line + 1) as i64 - 1;
        while at >= 0 {
            let index = at as usize;
            if self.alive(index) && self.range(index).1 >= line { found.push(index); }
            at = self.parents[index] as i64;
        }
        found
    }

    /// Indexes of the ranges touching lines `first ..= last`, outermost first.
    fn within(&self, first: u32, last: u32) -> Vec<usize> {
        let mut found = self.around(first);
        found.reverse();
        found.extend((self.heads.find(first + 1)..self.heads.find(last + 1)).filter(|&at| self.alive(at)));
        found
    }

    /// Keep the ranges on their lines through one edit (`gone` line breaks
    /// from `line`, `column` became `came`): ranges below move, ranges around
    /// it stretch, a range whose first line went dies. Returns whether a
    /// collapsed range changed, which the row mapping cannot follow by moving.
    fn follow(&mut self, line: u32, column: u32, gone: u32, came: u32) -> bool {
        let delta = came as i64 - gone as i64;
        let (first, last) = (line, line + gone);
        let low = self.heads.find(first + (column > 0) as u32);
        let mut changed = false;
        for at in low..self.heads.find(last) {
            if let Err(spot) = self.dead.binary_search(&(at as u32)) { self.dead.insert(spot, at as u32); }
            if let Ok(spot) = self.closed.binary_search(&(at as u32)) {
                self.closed.remove(spot);
                changed = true;
            }
            self.heads.put(at, last);
        }
        let mut at = low as i64 - 1;
        while at >= 0 {
            let index = at as usize;
            let (head, end) = self.range(index);
            let span = if end >= last {
                (self.spans[index] as i64 + delta) as u32
            } else if end >= first {
                (first + came).saturating_sub(head)
            } else {
                self.spans[index]
            };
            changed |= span != self.spans[index] && self.shut(index);
            self.spans[index] = span;
            at = self.parents[index] as i64;
        }
        self.heads.shift(low, delta);
        changed
    }

    /// The collapsed ranges, as lines.
    fn hidden(&self) -> impl Iterator<Item = (u32, u32)> + '_ {
        self.closed.iter().map(|&at| self.range(at as usize))
    }
}

/// Displayed rows over `lines` document lines. A hidden run is the lines
/// collapsed ranges hide together (nested and overlapping ones merge):
/// `start` its first line, `gone[i]` the lines hidden through run i. All
/// lookups are O(log runs).
#[derive(Default)]
struct Map {
    lines: u32,
    start: Lines,
    gone: Vec<u32>,
}

impl Map {
    /// From `(head, last)` ranges: `head` stays visible, `head + 1 ..= last` hide.
    fn new(lines: u32, mut ranges: Vec<(u32, u32)>) -> Map {
        let lines = lines.max(1);
        ranges.sort_unstable();
        let (mut start, mut gone, mut end, mut hidden) = (vec![], vec![], 0, 0);
        for (head, last) in ranges {
            let last = last.min(lines - 1);
            if last <= head { continue; }
            if !start.is_empty() && head <= end {
                // Its head is hidden by the previous run: one run covers both.
                if last > end {
                    hidden += last - end;
                    *gone.last_mut().unwrap() = hidden;
                    end = last;
                }
            } else {
                start.push(head + 1);
                hidden += last - head;
                gone.push(hidden);
                end = last;
            }
        }
        Map { lines, start: Lines::new(start), gone }
    }

    fn count(&self) -> u32 { self.lines - self.gone.last().copied().unwrap_or(0) }

    /// Lines hidden above run `run`.
    fn above(&self, run: usize) -> u32 { if run > 0 { self.gone[run - 1] } else { 0 } }

    fn last(&self, run: usize) -> u32 { self.start.line(run) + self.gone[run] - self.above(run) - 1 }

    /// The last run that starts at or above `line`.
    fn run(&self, line: u32) -> Option<usize> { self.start.find(line + 1).checked_sub(1) }

    fn line(&self, row: i64) -> u32 {
        let row = row.clamp(0, self.count() as i64 - 1) as u32;
        // The runs above the row: the line after run i is on row `start - above`.
        let (mut low, mut high) = (0, self.gone.len());
        while low < high {
            let run = (low + high) / 2;
            if self.start.line(run) - self.above(run) <= row { low = run + 1; } else { high = run; }
        }
        row + self.above(low)
    }

    /// The row showing `line`; a hidden line answers its fold head's row.
    fn row(&self, line: i64) -> u32 {
        let line = line.clamp(0, self.lines as i64 - 1) as u32;
        match self.run(line) {
            None => line,
            Some(run) if line <= self.last(run) => self.start.line(run) - 1 - self.above(run),
            Some(run) => line - self.gone[run],
        }
    }

    fn hidden(&self, line: u32) -> bool {
        self.run(line).is_some_and(|run| line <= self.last(run))
    }

    /// Lines hidden under `line` when it heads a run, else 0.
    fn folded(&self, line: u32) -> u32 {
        match self.run(line + 1) {
            Some(run) if self.start.line(run) == line + 1 => self.last(run) - line,
            _ => 0,
        }
    }

    /// Rows above `line`: the shown lines before it.
    fn before(&self, line: u32) -> u32 {
        match self.run(line) {
            None => line,
            Some(run) if line > self.last(run) => line - self.gone[run],
            Some(run) => self.start.line(run) - self.above(run),
        }
    }
}

/// The collapsed scope folds by key, with the name each is saved under.
pub type Shut = Keyed<Name>;

fn shut(facts: &Facts, names: &Shut) -> Vec<u32> {
    (0..facts.keys.len() as u32).filter(|&at| names.contains_key(&facts.keys[at as usize])).collect()
}

/// The default folds collapse.
fn seed(facts: &Facts, names: &mut Shut) {
    for &at in &facts.defaults {
        names.entry(facts.keys[at as usize]).or_insert_with(|| facts.name(at as usize));
    }
}

/// What the source's thread prepares for a session on the source: its own
/// copy of the fold ranges, and the collapsed ones among them.
pub struct Parcel {
    heads: Vec<u32>,
    spans: Vec<u32>,
    parents: Vec<i32>,
    closed: Vec<u32>,
    /// The session's collapsed names when it asked, and with its defaults applied.
    from: Arc<Shut>,
    into: Arc<Shut>,
}

impl Parcel {
    /// `seed_`: the session's default folds were not applied yet.
    pub fn new(facts: &Facts, from: Arc<Shut>, seed_: bool) -> Parcel {
        let mut into = from.clone();
        if seed_ { seed(facts, Arc::make_mut(&mut into)); }
        Parcel { heads: facts.heads.clone(), spans: facts.spans.clone(), parents: facts.parents.clone(),
                 closed: shut(facts, &into), from, into }
    }
}

#[pyclass(module = "meltygui.model._source_native")]
#[derive(Default)]
pub struct Rows {
    map: Map,
    /// Fold layers, lowest first: the scopes read from the text, the caller's ranges.
    layers: [Layer; 2],
    /// The reading the scope layer came from: its names, kinds and indents.
    facts: Option<Arc<Facts>>,
    pub(crate) shut: Arc<Shut>,
    /// The default folds were applied (they collapse once).
    pub(crate) seeded: bool,
    /// Caller ranges the last session left collapsed, until the caller's first ranges arrive.
    restored: Vec<(u32, u32)>,
    stamp: u64,
}

impl Rows {
    /// Build the row mapping again from the collapsed ranges of every layer.
    fn layout(&mut self) {
        self.map = Map::new(self.map.lines, self.layers.iter().flat_map(Layer::hidden).collect());
    }

    /// Collapse or expand range `at` of a layer (default: toggle).
    fn flip(&mut self, layer: usize, at: usize, close: Option<bool>) {
        let folds = &mut self.layers[layer];
        let spot = folds.closed.binary_search(&(at as u32));
        match (spot, close.unwrap_or(spot.is_err())) {
            (Err(spot), true) => folds.closed.insert(spot, at as u32),
            (Ok(spot), false) => { folds.closed.remove(spot); }
            _ => return,
        }
        if let (0, Some(facts)) = (layer, &self.facts) {
            let names = Arc::make_mut(&mut self.shut);
            if spot.is_err() { names.insert(facts.keys[at], facts.name(at)); } else { names.remove(&facts.keys[at]); }
            self.stamp += 1;
        }
    }

    /// What is drawn of the scope layer on lines `first ..= last`.
    fn sight(&self, first: u32, last: u32) -> Vec<(u32, u32, Option<(u32, bool)>, bool)> {
        let (folds, Some(facts)) = (&self.layers[0], &self.facts) else { return vec![] };
        folds.within(first, last).into_iter().map(|at| {
            let (head, end) = folds.range(at);
            (head, end, facts.scope(at), folds.shut(at))
        }).collect()
    }
}

#[pymethods]
impl Rows {
    /// `collapsed`: the scope fold names a session left collapsed; `seeded`:
    /// the default folds stay as they are; `gaps`: the caller ranges it left collapsed.
    #[new]
    #[pyo3(signature = (lines=1, collapsed=vec![], seeded=false, gaps=vec![]))]
    fn new(lines: u32, collapsed: Vec<Bound<'_, PyAny>>, seeded: bool, gaps: Vec<(u32, u32)>) -> PyResult<Rows> {
        let mut names = Shut::default();
        for item in collapsed {
            let mut name = Name { parts: vec![], twin: 0 };
            for part in item.try_iter()? {
                let part = part?;
                match part.extract::<String>() {
                    Ok(text) => name.parts.push(text.into()),
                    Err(_) => name.twin = part.extract()?,
                }
            }
            names.insert(name.key(), name);
        }
        Ok(Rows { map: Map::new(lines, vec![]), shut: Arc::new(names), seeded, restored: gaps, ..Rows::default() })
    }

    /// Rows in all: the lines no fold hides.
    #[getter]
    fn count(&self) -> u32 { self.map.count() }

    /// Hidden runs: 0 when every line is shown.
    #[getter]
    fn runs(&self) -> usize { self.map.gone.len() }

    /// Rises when a scope fold opened or closed: `collapsed` is to be saved again.
    #[getter]
    fn stamp(&self) -> u64 { self.stamp }

    /// The document line displayed on `row` (clamped).
    fn line(&self, row: i64) -> u32 { self.map.line(row) }

    /// The row showing `line`; a hidden line answers its fold head's row.
    fn row(&self, line: i64) -> u32 { self.map.row(line) }

    fn hidden(&self, line: u32) -> bool { self.map.hidden(line) }

    /// Lines hidden under `line` when it heads a collapsed run, else 0.
    fn folded(&self, line: u32) -> u32 { self.map.folded(line) }

    /// Rows above `line`: the shown lines before it.
    fn before(&self, line: u32) -> u32 { self.map.before(line) }

    /// Document lines of rows `start .. stop - 1`.
    fn span(&self, start: i64, stop: i64) -> Vec<u32> {
        (start.max(0)..stop.min(self.map.count() as i64)).map(|row| self.map.line(row)).collect()
    }

    /// (layer, it is collapsed) of the fold headed by `line`, or None.
    fn fold(&self, line: u32) -> Option<(usize, bool)> {
        self.layers.iter().enumerate().find_map(|(layer, folds)| folds.at(line).map(|at| (layer, folds.shut(at))))
    }

    /// Collapse or expand the fold headed by `line`. False when there is none.
    fn toggle(&mut self, line: u32) -> bool {
        let Some((layer, at)) = (0..2).find_map(|layer| self.layers[layer].at(line).map(|at| (layer, at))) else { return false };
        self.flip(layer, at, None);
        self.layout();
        true
    }

    /// Collapse (or expand) the scope folds headed by `heads`, however
    /// many, with one layout: what a fold chord does to its targets
    /// (editor/keys.py). True when one changed.
    fn set(&mut self, heads: Vec<u32>, close: bool) -> bool {
        let folds = &mut self.layers[0];
        let mut flipped: Vec<u32> = heads.into_iter().filter_map(|line| folds.at(line)).map(|at| at as u32)
            .filter(|&at| folds.shut(at as usize) != close).collect();
        flipped.sort_unstable();
        flipped.dedup();
        if flipped.is_empty() { return false; }
        if close {
            folds.closed.extend(&flipped);
            folds.closed.sort_unstable();
        } else {
            folds.closed.retain(|at| flipped.binary_search(at).is_err());
        }
        if let Some(facts) = &self.facts {
            let names = Arc::make_mut(&mut self.shut);
            for &at in &flipped {
                let at = at as usize;
                if close { names.insert(facts.keys[at], facts.name(at)); } else { names.remove(&facts.keys[at]); }
            }
            self.stamp += 1;
        }
        self.layout();
        true
    }

    /// The first lines of the scope folds the collapse-all and expand-all
    /// chords act on, the legacy editor's rule: the first nesting level that
    /// holds two folds or more (a file that is one class is looked through
    /// to its members) and the level inside each of those; the folds that
    /// start collapsed (strings, comment runs, imports) count for no level,
    /// are collapsed wherever they are and never expanded.
    fn levels(&self, close: bool) -> Vec<u32> {
        let (folds, Some(facts)) = (&self.layers[0], &self.facts) else { return vec![] };
        let default = |at: usize| matches!(facts.kinds[at], STR | COMMENT | IMPORTS);
        // The live range around each one (a range whose first line was deleted is none until the next reading).
        let around = |at: usize| {
            let mut up = folds.parents[at];
            while up >= 0 && !folds.alive(up as usize) { up = folds.parents[up as usize]; }
            up
        };
        let live: Vec<(usize, i32)> = (0..folds.spans.len()).filter(|&at| folds.alive(at)).map(|at| (at, around(at))).collect();
        let mut top = -1;
        loop {
            let mut counted = live.iter().filter(|&&(at, up)| up == top && !default(at));
            let (Some(&(lone, _)), None) = (counted.next(), counted.next()) else { break };
            if !live.iter().any(|&(_, up)| up == lone as i32) { break; }
            top = lone as i32;
        }
        let mut root = vec![false; folds.spans.len()];
        for &(at, up) in &live { root[at] = up == top; }
        live.iter().filter(|&&(at, up)| if default(at) { close } else { root[at] || (up >= 0 && root[up as usize]) })
            .map(|&(at, _)| folds.heads.line(at)).collect()
    }

    /// Collapse or expand the outermost scope ranges.
    fn fold_all(&mut self, close: bool) {
        let Some(facts) = self.facts.clone() else { return };
        let (folds, names) = (&mut self.layers[0], Arc::make_mut(&mut self.shut));
        for at in (0..folds.spans.len()).filter(|&at| folds.parents[at] < 0 && folds.alive(at)) {
            if close { names.entry(facts.keys[at]).or_insert_with(|| facts.name(at)); } else { names.remove(&facts.keys[at]); }
        }
        folds.closed = shut(&facts, names);
        folds.closed.retain(|&at| folds.dead.binary_search(&at).is_err());
        self.stamp += 1;
        self.layout();
    }

    /// Open every collapsed range hiding `line`. True if any opened.
    fn reveal(&mut self, line: u32) -> bool {
        let mut any = false;
        for layer in 0..2 {
            let folds = &self.layers[layer];
            let hiding: Vec<usize> = folds.around(line).into_iter()
                .filter(|&at| folds.shut(at) && folds.heads.line(at) < line).collect();
            for at in hiding {
                self.flip(layer, at, Some(false));
                any = true;
            }
        }
        if any { self.layout(); }
        any
    }

    /// The first line of the innermost open scope range around `line`, or None.
    fn enclosing(&self, line: u32) -> Option<u32> {
        let folds = &self.layers[0];
        folds.around(line).into_iter().find(|&at| !folds.shut(at)).map(|at| folds.heads.line(at))
    }

    /// (first line, last line, indent in spaces, it is a def or class) of the
    /// scopes and blocks touching lines `first ..= last` whose first line is
    /// shown, outermost first: what the view draws guides for.
    fn guides(&self, first: u32, last: u32) -> Vec<(u32, u32, u32, bool)> {
        self.sight(first, last).into_iter().filter_map(|(head, end, scope, _)| {
            scope.filter(|_| !self.map.hidden(head)).map(|(indent, def)| (head, end, indent, def))
        }).collect()
    }

    /// Follow one edit: the text from (`line`, `column`) over `gone` line
    /// breaks became text over `came` line breaks.
    fn follow(&mut self, line: u32, column: u32, gone: u32, came: u32) {
        if gone == 0 && came == 0 { return; }
        let changed = self.layers.iter_mut().fold(false, |any, folds| folds.follow(line, column, gone, came) | any);
        self.map.lines = (self.map.lines as i64 + came as i64 - gone as i64) as u32;
        if changed {
            self.layout();
        } else {
            // No collapsed range changed: the hidden runs below the edit only move.
            let below = self.map.start.find(line + gone + 1);
            self.map.start.shift(below, came as i64 - gone as i64);
        }
    }

    /// Take the scope ranges `source` landed and bring them forward over
    /// `changes`, the edits made since as `follow` arguments. The source's
    /// thread prepared them for every session it was asked with
    /// (`Source.ask`); a session it did not know, or one that toggled a fold
    /// meanwhile, finds its collapsed ranges here. Returns whether what is
    /// drawn on lines `first ..= last` changed.
    fn land(&mut self, source: &Bound<'_, Source>, changes: Vec<(u32, u32, u32, u32)>, first: u32, last: u32) -> bool {
        let mut source = source.borrow_mut();
        let Some(found) = source.landed.as_mut() else { return false };
        let facts = found.facts.clone();
        let before = self.sight(first, last);
        let mine = found.folds.iter().position(|parcel| Arc::ptr_eq(&parcel.from, &self.shut)).map(|at| found.folds.swap_remove(at));
        let (heads, spans, parents, closed) = match mine {
            Some(parcel) => {
                self.shut = parcel.into;
                (parcel.heads, parcel.spans, parcel.parents, parcel.closed)
            }
            None => {
                if !self.seeded { seed(&facts, Arc::make_mut(&mut self.shut)); }
                (facts.heads.clone(), facts.spans.clone(), facts.parents.clone(), shut(&facts, &self.shut))
            }
        };
        if !self.seeded { self.stamp += 1; }
        self.seeded = true;
        self.layers[0] = Layer { heads: Lines::new(heads), spans, parents, dead: vec![], closed };
        if let Some(old) = self.facts.replace(facts) { crate::retire(old); }
        for (line, column, gone, came) in changes {
            if gone > 0 || came > 0 { self.layers[0].follow(line, column, gone, came); }
        }
        self.layout();
        before != self.sight(first, last)
    }

    /// Set the caller's layer from `(head, last)` line ranges. A range starts
    /// collapsed, unless it overlaps one the user expanded: the ranges come
    /// from a live diff and drift as the text changes. (The first ranges
    /// collapse as the last session left them.)
    fn gaps(&mut self, mut ranges: Vec<(u32, u32)>) {
        let folds = &self.layers[1];
        let open: Vec<(u32, u32)> = (0..folds.spans.len()).filter(|&at| folds.alive(at) && !folds.shut(at))
            .map(|at| folds.range(at)).collect();
        let restored = if folds.spans.is_empty() && !ranges.is_empty() { std::mem::take(&mut self.restored) } else { vec![] };
        ranges.sort_unstable_by_key(|&(head, last)| (head, u32::MAX - last));
        let closed = (0..ranges.len() as u32).filter(|&at| {
            let (head, last) = ranges[at as usize];
            if restored.is_empty() { !open.iter().any(|&(low, high)| low <= last && head <= high) } else { restored.contains(&(head, last)) }
        }).collect();
        self.layers[1] = Layer {
            heads: Lines::new(ranges.iter().map(|range| range.0).collect()),
            spans: ranges.iter().map(|&(head, last)| last.saturating_sub(head)).collect(),
            parents: nest(ranges.iter().copied()), dead: vec![], closed,
        };
        self.layout();
    }

    /// The collapsed ranges of a layer as (first line, last line).
    #[pyo3(signature = (layer=0))]
    fn closed(&self, layer: usize) -> Vec<(u32, u32)> { self.layers[layer].hidden().collect() }

    /// Every range of a layer as (first line, last line), outermost first.
    #[pyo3(signature = (layer=0))]
    fn ranges(&self, layer: usize) -> Vec<(u32, u32)> {
        let folds = &self.layers[layer];
        (0..folds.spans.len()).filter(|&at| folds.alive(at)).map(|at| folds.range(at)).collect()
    }

    /// The names of the scope ranges, in the order of `ranges`.
    fn names<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let names = PyList::empty(py);
        if let Some(facts) = &self.facts {
            for at in (0..self.layers[0].spans.len()).filter(|&at| self.layers[0].alive(at)) {
                names.append(crate::name(py, &facts.name(at))?)?;
            }
        }
        Ok(names)
    }

    /// The names of the collapsed scope folds, in an order of their own
    /// (the same set, the same list): what a session saves.
    fn collapsed<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let mut found: Vec<&Name> = self.shut.values().collect();
        found.sort_unstable_by(|a, b| (&a.parts, a.twin).cmp(&(&b.parts, b.twin)));
        let names = PyList::empty(py);
        for item in found { names.append(crate::name(py, item)?)?; }
        Ok(names)
    }
}

/// Search hits (line, column, end column) that stay on their text through edits.
#[pyclass(module = "meltygui.model._source_native")]
#[derive(Clone)]
pub struct Hits {
    lines: Lines,
    spans: Vec<(u32, u32)>,
}

impl Hits {
    pub fn new(found: Vec<(u32, u32, u32)>) -> Hits {
        Hits { lines: Lines::new(found.iter().map(|hit| hit.0).collect()), spans: found.iter().map(|hit| (hit.1, hit.2)).collect() }
    }

    /// Index of the first hit at or after (line, column).
    fn spot(&self, line: u32, column: u32) -> usize {
        let mut at = self.lines.find(line);
        while at < self.spans.len() && self.lines.line(at) == line && self.spans[at].0 < column { at += 1; }
        at
    }
}

#[pymethods]
impl Hits {
    fn __len__(&self) -> usize { self.spans.len() }

    /// The hits on lines `first ..= last`.
    pub fn on(&self, first: u32, last: u32) -> Vec<(u32, u32, u32)> {
        (self.lines.find(first)..self.lines.find(last.saturating_add(1)))
            .map(|at| (self.lines.line(at), self.spans[at].0, self.spans[at].1)).collect()
    }

    /// Index of the first hit at or after (line, column): `len` when there
    /// is none. A bisection of the lines, whatever the number of hits: what
    /// find next and previous step by.
    fn seek(&self, line: u32, column: u32) -> usize { self.spot(line, column) }

    /// Hit `index` as (line, column, end column), or None.
    fn at(&self, index: usize) -> Option<(u32, u32, u32)> {
        self.spans.get(index).map(|span| (self.lines.line(index), span.0, span.1))
    }

    /// Follow one edit (a `Splice`, editor/document.py): hits inside the
    /// removed text go, hits after it move, one the edit starts in ends there
    /// (or grows with text typed inside it).
    fn follow(&mut self, line: u32, column: u32, gone: u32, old: u32, came: u32, new: u32) {
        let last = line + gone;
        let (low, high) = (self.spot(line, column), self.spot(last, old));
        for at in self.lines.find(line)..low {
            let end = &mut self.spans[at].1;
            if *end > column { *end = if gone == 0 && *end >= old { *end - old + new } else { column }; }
        }
        if high > low {
            self.spans.drain(low..high);
            self.lines.cut(low, high);
        }
        let mut at = low;
        while new != old && at < self.spans.len() && self.lines.line(at) == last {
            let (start, end) = self.spans[at];
            self.spans[at] = ((start + new).saturating_sub(old), (end + new).saturating_sub(old));
            at += 1;
        }
        self.lines.shift(low, came as i64 - gone as i64);
    }
}
