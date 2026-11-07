"""Build opt-in timing probes in an isolated copy of the native GUI extension.

Never overwrites the checkout's extension or target directory. The result is
loaded only by profile_gui_overhead.py --native-path. Timers report CPU wall
intervals, including driver calls, and are not GPU execution-time measurements.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


LABELS = [
    'renderer.argument_dict', 'renderer.identity_state', 'renderer.geometry_events',
    'renderer.parameters', 'renderer.group_enter', 'renderer.cleanup', 'renderer.return',
    'renderer.total_outside_body', 'state.configure', 'identity.handle', 'identity.resolve',
    'graph.declare', 'graph.commit', 'graph.associate', 'graph.dirty_nodes', 'graph.visible',
    'graph.visible_children', 'graph.measured', 'texture.target', 'texture.add_packet',
    'texture.render_many', 'texture.render_inner', 'texture.setup', 'texture.packet_upload_draw',
    'texture.child_composition', 'texture.resolve', 'layout.fork', 'graph.composition_order',
    'graph.invalidate', 'graph.result', 'graph.begin', 'graph.clip_rect', 'graph.rect',
    'gl.buffer_data', 'gl.draw_elements', 'gl.bind_framebuffer', 'gl.clear',
]


def replace_once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)


def mark(label):
    return f'\n        probe::mark(&mut profile_mark, {LABELS.index(label)});\n'


def instrument(destination, source):
    destination.mkdir(parents=True, exist_ok=False)
    for name in ('Cargo.toml', 'Cargo.lock'):
        shutil.copy2(source / name, destination / name)
    shutil.copytree(source / 'src', destination / 'src')
    labels = ','.join(json.dumps(label) for label in LABELS)
    (destination / 'src/probe.rs').write_text('''
use std::cell::{Cell, RefCell};
use std::time::Instant;
use pyo3::prelude::*;
use pyo3::types::PyDict;
const LABELS: &[&str] = &[''' + labels + '''];
thread_local! {
    static ENABLED: Cell<bool> = const {Cell::new(false)};
    static VALUES: RefCell<Vec<(u64,u64)>> = RefCell::new(vec![(0,0); LABELS.len()]);
}
pub fn record(index:usize, ns:u64) {
    if ENABLED.with(|enabled| enabled.get()) {
        VALUES.with(|values| {let mut values=values.borrow_mut(); values[index].0+=1; values[index].1+=ns;});
    }
}
pub fn mark(start:&mut Instant,index:usize) {
    let now=Instant::now();record(index,now.duration_since(*start).as_nanos() as u64);*start=Instant::now();
}
pub struct Span { start: Option<Instant>, index:usize, pub excluded:u64 }
impl Span {
    pub fn new(index:usize)->Self {Self {start:ENABLED.with(|e| if e.get(){Some(Instant::now())}else{None}),index,excluded:0}}
}
impl Drop for Span {
    fn drop(&mut self) {if let Some(start)=self.start {record(self.index,(start.elapsed().as_nanos() as u64).saturating_sub(self.excluded));}}
}
#[pyfunction]
pub fn profile_reset() {VALUES.with(|v|v.borrow_mut().fill((0,0)));ENABLED.with(|e|e.set(true));}
#[pyfunction]
pub fn profile_snapshot(py:Python<'_>)->PyResult<Py<PyDict>> {
    ENABLED.with(|e|e.set(false));let result=PyDict::new(py);
    VALUES.with(|v|->PyResult<()> {for (label,(calls,ns)) in LABELS.iter().zip(v.borrow().iter()) {result.set_item(label,(*calls,*ns as f64/1000.))?;}Ok(())})?;
    Ok(result.unbind())
}
''')
    path = destination / 'src/lib.rs'
    text = path.read_text()
    first, rest = text.split('\n', 1)
    text = first + '\nmod probe;\n' + rest
    text = replace_once(text, '        let start = Instant::now();',
                        '        let mut profile_total = probe::Span::new(7);\n'
                        '        let start = Instant::now();\n        let mut profile_mark = start;')
    anchors = [
        ('        let (state, unique, unique_value, inherited_width, imgui, owner, globals) = {', 'renderer.argument_dict'),
        ('        for (key, value) in globals.bind(py).iter() {', 'renderer.identity_state'),
        ('        let clean = if self.var_kwargs {', 'renderer.geometry_events'),
        ('        if let Some(imgui) = &imgui {\n            imgui\n                .bind(py)\n                .call_method1("push_id",', 'renderer.parameters'),
        ('        let body_start = Instant::now();', 'renderer.group_enter'),
        ('        let result = result?;\n        cleanup?;', 'renderer.cleanup'),
    ]
    for anchor, label in anchors:
        text = replace_once(text, anchor, mark(label) + anchor)
    text = replace_once(text, '        let body_ns = body_start.elapsed().as_nanos() as u64;',
                        '        let body_ns = body_start.elapsed().as_nanos() as u64;\n'
                        '        profile_total.excluded = body_ns;\n        profile_mark = Instant::now();')
    text = replace_once(text, '        let result = result?;\n        cleanup?;',
                        '        let _return_probe = probe::Span::new(6);\n        let result = result?;\n        cleanup?;')
    text = replace_once(text, '    m.add_class::<Renderer>()?;',
                        '    m.add_function(wrap_pyfunction!(probe::profile_reset, m)?)?;\n'
                        '    m.add_function(wrap_pyfunction!(probe::profile_snapshot, m)?)?;\n    m.add_class::<Renderer>()?;')
    path.write_text(text)
    scopes = {
        'lib.rs': ['configure'], 'identity.rs': ['handle', 'resolve'],
        'retained.rs': ['declare', 'commit', 'associate', 'dirty_nodes', 'visible', 'visible_children',
                        'measured', 'composition_order', 'invalidate', 'result', 'begin', 'clip_rect', 'rect'],
        'texture.rs': ['target', 'add_packet', 'render_many', 'render_inner'], 'edges.rs': ['fork'],
    }
    prefixes = {'lib.rs': 'state', 'identity.rs': 'identity', 'retained.rs': 'graph',
                'texture.rs': 'texture', 'edges.rs': 'layout'}
    for filename, functions in scopes.items():
        path = destination / 'src' / filename
        text = path.read_text()
        for function in functions:
            pattern = r'(\bfn ' + function + r'\b[\s\S]*?\{)'
            matches = list(re.finditer(pattern, text))
            assert len(matches) == 1, (filename, function, len(matches))
            index = LABELS.index(prefixes[filename] + '.' + function)
            text = re.sub(pattern, lambda m: m[0] + f'\n        let _profile_span = crate::probe::Span::new({index});', text, count=1)
        path.write_text(text)
    path = destination / 'src/texture.rs'
    text = path.read_text()
    text = replace_once(text, '            let target = self\n                .targets',
                        '            let mut profile_mark = std::time::Instant::now();\n            let target = self\n                .targets')
    anchors = [('            for packet in self\n', 'texture.setup'),
               ('            // Viewport contents are selected', 'texture.packet_upload_draw'),
               ('            // Resolve premultiplied accumulation', 'texture.child_composition'),
               ('            self.renders += 1;', 'texture.resolve')]
    for anchor, label in anchors:
        text = replace_once(text, anchor, f'            crate::probe::mark(&mut profile_mark, {LABELS.index(label)});\n' + anchor)
    functions = [
        ('glBufferData', 'buffer_data', 't:u32, size:isize, data:*const c_void, usage:u32', 't,size,data,usage'),
        ('glDrawElementsBaseVertex', 'draw_elements', 'mode:u32, count:i32, typ:u32, data:*const c_void, base:i32', 'mode,count,typ,data,base'),
        ('glBindFramebuffer', 'bind_framebuffer', 'target:u32, framebuffer:u32', 'target,framebuffer'),
        ('glClear', 'clear', 'mask:u32', 'mask'),
    ]
    for name, label, signature, arguments in functions:
        text = re.sub(r'(?<!fn )\b' + name + r'\(', 'profile_' + label + '(', text)
        index = LABELS.index('gl.' + label)
        text += f'\nunsafe fn profile_{label}({signature}) {{\n'
        text += f'    let _profile_span = crate::probe::Span::new({index});\n    {name}({arguments});\n}}\n'
    path.write_text(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    instrument(args.destination, root / 'native/gui_prototype')
    cargo = shutil.which('cargo')
    import os
    subprocess.run([cargo, 'build', '--release', '--locked', '--manifest-path', str(args.destination / 'Cargo.toml')],
                   check=True, close_fds=False, env={**os.environ, 'PYO3_PYTHON': sys.executable,
                                                    'CARGO_TARGET_DIR': str(args.destination / 'target')})
    print(args.destination / 'target/release/lib_gui_native.so')


if __name__ == '__main__':
    main()
