"""Isolated graph revisions and compact execution metadata for follow-up tests."""
import argparse
from pathlib import Path
import shutil
import subprocess


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args();dest=args.destination.resolve();dest.mkdir(parents=True,exist_ok=False)
    source=Path(__file__).resolve().parents[1]/'native/gui_prototype'
    for name in ('Cargo.toml','Cargo.lock'):shutil.copy2(source/name,dest/name)
    shutil.copytree(source/'src',dest/'src')
    path=dest/'src/edges.rs';s=path.read_text()
    s=s.replace('    visits: usize,','    visits: usize,\n    position_revision: u64,')
    s=s.replace('            visits: 0,','            visits: 0,\n            position_revision: 0,')
    assert s.count('        Ok(self.delta(&before))')==3
    s=s.replace('        Ok(self.delta(&before))','''        let changes = self.delta(&before);
        if !changes.is_empty() { self.position_revision += 1; }
        Ok(changes)''')
    s=s.replace('    fn stats(&self)', '    fn revision(&self) -> (u64,u64) { (self.version,self.position_revision) }\n    fn stats(&self)')
    path.write_text(s)
    path=dest/'src/retained.rs';s=path.read_text();needle='    fn info(&mut self, py: Python<'
    at=s.index(needle)
    s=s[:at]+'''    fn execution(&mut self, py: Python<'_>, id: u64) -> PyResult<(Py<PyAny>, Py<PyDict>, (f64,f64,f64,f64), f64)> {
        let (input,args) = self.invocation(py,id)?;
        self.sync(py);
        let rect = self.geometry.rect(py,id);
        let height = if self.nodes[&id].auto_height {-1.} else {rect.3};
        Ok((input,args,rect,height))
    }
'''+s[at:];path.write_text(s)
    cargo=shutil.which('cargo');assert cargo
    subprocess.run([cargo,'build','--release','--locked','--manifest-path',str(dest/'Cargo.toml')],check=True,close_fds=False)


if __name__=='__main__':main()
