"""Build cohesive declaration prototype outside the installed extension."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--destination',required=True,type=Path)
    parser.add_argument('--result-probe',action='store_true',help='Test-only native result-read counter; never used for performance')
    args=parser.parse_args();dest=args.destination.resolve();dest.mkdir(parents=True,exist_ok=False)
    root=Path(__file__).resolve().parents[1];source=root/'native/gui_prototype'
    for name in ('Cargo.toml','Cargo.lock'):shutil.copy2(source/name,dest/name)
    shutil.copytree(source/'src',dest/'src')
    shutil.copy2(Path(__file__).with_name('native_declaration_prototype.rs'),dest/'src/declaration.rs')
    path=dest/'src/lib.rs';s=path.read_text();first,rest=s.split('\n',1);s=first+'\nmod declaration;\n'+rest
    s=s.replace('    m.add_class::<Renderer>()?;', '    m.add_class::<declaration::RetainedDeclaration>()?;\n    m.add_class::<Renderer>()?;')
    path.write_text(s)
    path=dest/'src/retained.rs';s=path.read_text()
    for name in ['same_argument','declare','constraints','pending','result','invalidate_node','dissociate','associate','dirty','is_scrollable','visible','rect']:
        pattern=r'(?m)^(\s*)fn '+name+r'\('
        s,n=re.subn(pattern,lambda m:m[1]+'pub(crate) fn '+name+'(',s)
        assert n==1,(name,n)
    if args.result_probe:
        s=s.replace('    retired: Vec<u64>,','    retired: Vec<u64>,\n    test_result_reads: [usize;2],')
        s=s.replace('            retired: vec![],','            retired: vec![],\n            test_result_reads: [0,0],')
        needle="    pub(crate) fn result(&mut self, py: Python<'_>, id: u64, consume: bool) -> PyResult<(bool, Py<PyAny>)> {"
        assert s.count(needle)==1
        s=s.replace(needle,'''    fn result_read_counts(&self)->(usize,usize) {(self.test_result_reads[0],self.test_result_reads[1])}
    fn reset_result_reads(&mut self) {self.test_result_reads=[0,0];}
'''+needle+'\n        self.test_result_reads[consume as usize]+=1;')
    path.write_text(s)
    cargo=shutil.which('cargo');assert cargo
    subprocess.run([cargo,'build','--release','--locked','--manifest-path',str(dest/'Cargo.toml')],check=True,close_fds=False)


if __name__=='__main__':main()
