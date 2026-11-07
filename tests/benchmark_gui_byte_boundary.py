"""Isolate pinned PyO3 Vec<u8> ingress versus typed bytes plus owned copies."""
import argparse
import json
from pathlib import Path
import random
import shutil
import subprocess
from time import perf_counter_ns


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    source=Path(__file__).resolve().parents[1]/'native/gui_prototype'
    destination=args.destination.resolve();destination.mkdir(parents=True,exist_ok=False)
    for name in ('Cargo.toml','Cargo.lock'):shutil.copy2(source/name,destination/name)
    shutil.copytree(source/'src',destination/'src')
    path=destination/'src/lib.rs';text=path.read_text()
    needle='    m.add_class::<Renderer>()?;'
    assert text.count(needle)==1
    text=text.replace(needle,'''    m.add_function(wrap_pyfunction!(byte_probe_vec,m)?)?;
    m.add_function(wrap_pyfunction!(byte_probe_typed,m)?)?;
'''+needle)
    text+='''
fn byte_probe_owned(vertices: Vec<u8>, indices: Vec<u8>) -> (usize,usize,u8) {
    std::hint::black_box((&vertices,&indices));
    (vertices.len(),indices.len(),vertices[0]^indices[indices.len()-1])
}
#[pyfunction]
fn byte_probe_vec(vertices: Vec<u8>, indices: Vec<u8>) -> (usize,usize,u8) {
    byte_probe_owned(vertices,indices)
}
#[pyfunction]
fn byte_probe_typed(vertices: &Bound<'_,pyo3::types::PyBytes>, indices: &Bound<'_,pyo3::types::PyBytes>) -> (usize,usize,u8) {
    byte_probe_owned(vertices.as_bytes().to_vec(),indices.as_bytes().to_vec())
}
'''
    path.write_text(text)
    cargo=shutil.which('cargo');assert cargo
    subprocess.run([cargo,'build','--release','--locked','--manifest-path',str(destination/'Cargo.toml')],
                   close_fds=False,check=True)
    from profile_gui_overhead import load_native
    load_native(destination/'target/release/lib_gui_native.so')
    from meltygui.core.rendering import _gui_native
    from benchmark_gui_overhead import summary
    rng=random.Random(44);report={}
    for vertices,indices,calls in [(80,24,341),(1280,384,341),(49152,16384,16),(393216,131072,1)]:
        v=bytes([71])*vertices;i=bytes([93])*indices
        samples={'vec':[],'typed_bytes':[]}
        for frame in range(120):
            order=list(samples);rng.shuffle(order)
            for mode in order:
                function=_gui_native.byte_probe_vec if mode=='vec' else _gui_native.byte_probe_typed
                start=perf_counter_ns()
                for _ in range(calls):result=function(v,i)
                elapsed=(perf_counter_ns()-start)/1000/calls
                assert result==(vertices,indices,71^93)
                if frame>=20:samples[mode].append(elapsed)
        report[str(vertices+indices)]=dict(vertices=vertices,indices=indices,calls=calls,
            per_call_us={k:summary(values) for k,values in samples.items()},samples_us=samples)
        print(vertices+indices,report[str(vertices+indices)]['per_call_us'],flush=True)
    args.output.write_text(json.dumps(report,indent=2))


if __name__=='__main__':main()
