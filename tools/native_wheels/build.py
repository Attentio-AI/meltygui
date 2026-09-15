"""Build Linux x86-64/CPython 3.12 support wheels on the pinned manylinux image."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from prepare import prepare

IMAGE = 'quay.io/pypa/manylinux_2_28_x86_64@sha256:531d7aa844bbb0c131d4ab011d3db741c4abc8d498cd5ccc86121046f62303b4'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=['imgui', 'pycuda'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cuda-root', type=Path)
    parser.add_argument('--sudo-docker', action='store_true')
    args = parser.parse_args()
    if args.kind == 'pycuda' and (not args.cuda_root or not (args.cuda_root / 'include/cuda.h').exists()):
        parser.error('PyCUDA needs --cuda-root pointing at a CUDA 12.1 toolkit')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    docker = ['sudo', '-n', 'docker'] if args.sudo_docker else ['docker']
    with tempfile.TemporaryDirectory(prefix='meltygui-native-') as work:
        source = Path(work) / args.kind
        prepare(args.kind, source)
        command = [*docker, 'run', '--rm', '--platform', 'linux/amd64',
                   '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'HOME=/tmp/build-home',
                   '-v', f'{source}:/source', '-v', f'{output}:/output',
                   '-v', f'{Path(__file__).resolve().parent}:/tools:ro']
        if args.cuda_root:
            command += ['-v', f'{args.cuda_root.resolve()}:/usr/local/cuda-12.1:ro']
        subprocess.run([*command, IMAGE, 'bash', '/tools/build_in_container.sh', args.kind], check=True)
        upstream = json.loads((source / 'UPSTREAM.json').read_text())
    artifacts = [p for p in output.iterdir() if p.suffix in ('.whl', '.gz')]
    receipt = {'kind': args.kind, 'upstream': upstream, 'build_image': IMAGE,
               'python': 'CPython 3.12', 'target': 'manylinux_2_28_x86_64',
               'sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts}}
    (output / 'build.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
