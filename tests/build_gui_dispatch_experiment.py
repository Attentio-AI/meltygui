"""Build isolated metadata and packet-copy bridges from current workspace source."""
import argparse
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--fused-dispatch', action='store_true')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'native/gui_prototype'
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name in ('Cargo.toml', 'Cargo.lock'):
        shutil.copy2(source / name, destination / name)
    shutil.copytree(source / 'src', destination / 'src')
    path = destination / 'src/retained.rs'
    source_text = path.read_text()
    needle = '    fn info(&mut self, py: Python<'
    assert source_text.count(needle) == 1
    index = source_text.index(needle)
    source_text = source_text[:index] + '''    fn execution(&mut self, py: Python<'_>, id: u64) -> PyResult<(Py<PyAny>, Py<PyDict>, (f64,f64,f64,f64), f64)> {
        self.sync(py);
        let (input, args) = self.invocation(py,id)?;
        let r = self.geometry.rect(py,id);
        let height = if self.nodes[&id].auto_height {-1.} else {r.3};
        Ok((input,args,r,height))
    }
''' + source_text[index:]
    path.write_text(source_text)
    if args.fused_dispatch:
        path = destination / 'src/lib.rs'
        source_text = path.read_text()
        needle = '    #[pyo3(signature = (input_value=None, **kwargs))]\n'
        assert source_text.count(needle) == 1
        index = source_text.index(needle)
        source_text = source_text[:index] + '''    fn retained_execute(&self, py: Python<'_>, input: Py<PyAny>, arguments: &Bound<'_, PyDict>,
        unique: Py<PyAny>, explicit: Py<PyAny>, cache: Py<PyAny>, events: Py<PyAny>, width: f64) -> PyResult<Py<PyAny>> {
        let args = arguments.copy()?;
        args.set_item("return_extras", true)?;
        args.set_item("_gui_unique", unique)?;
        args.set_item("_gui_explicit", explicit)?;
        let globals = PyDict::new(py);
        globals.set_item("cache", cache)?;
        globals.set_item("view_events", events)?;
        self.runtime.borrow_mut(py).begin_frame(py, None, width, None, false, Some(&globals))?;
        let result = self.__call__(py, Some(input), Some(&args));
        let ended = self.runtime.borrow_mut(py).end_frame(py);
        ended?;
        result
    }
''' + source_text[index:]
        path.write_text(source_text)
    path = destination / 'src/texture.rs'
    source_text = path.read_text()
    needle = '    fn add_packet(\n'
    assert source_text.count(needle) == 1
    index = source_text.index(needle)
    source_text = source_text[:index] + '''    // Experimental render-thread-only bridge: copy while ImGui buffers are alive.
    fn add_packet_pointer(&mut self, id: u64, vertex_ptr: usize, vertex_len: usize,
        index_ptr: usize, index_len: usize, commands: Vec<Command>) -> PyResult<()> {
        if (vertex_len > 0 && vertex_ptr == 0) || (index_len > 0 && index_ptr == 0) {
            return Err(PyRuntimeError::new_err("null packet buffer"));
        }
        let vertices = if vertex_len == 0 {vec![]} else {
            unsafe {std::slice::from_raw_parts(vertex_ptr as *const u8, vertex_len).to_vec()}
        };
        let indices = if index_len == 0 {vec![]} else {
            unsafe {std::slice::from_raw_parts(index_ptr as *const u8, index_len).to_vec()}
        };
        self.add_packet(id, vertices, indices, commands)
    }
''' + source_text[index:]
    path.write_text(source_text)
    cargo = shutil.which('cargo')
    if cargo is None:
        raise SystemExit('cargo required')
    subprocess.run([cargo, 'build', '--release', '--locked', '--manifest-path',
                    str(destination / 'Cargo.toml')], check=True, close_fds=False)
    print(destination / 'target/release/lib_gui_native.so')


if __name__ == '__main__':
    main()
