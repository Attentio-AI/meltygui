#!/usr/bin/env python3
"""Build the pinned GL-enabled PyCUDA sdist and wheel with uv."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cuda-root', type=Path, required=True)
    parser.add_argument('--python', default='3.12')
    parser.add_argument('--source-repo', default='https://github.com/inducer/pycuda.git')
    args = parser.parse_args()
    revision = '19bed9035edb9990b2928e297125d8e9d470130b'
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    toolkit = args.cuda_root.resolve()
    if not (toolkit / 'bin/nvcc').is_file():
        parser.error('--cuda-root must contain bin/nvcc')
    environment = dict(os.environ, PATH=str(toolkit / 'bin') + os.pathsep + os.environ['PATH'])
    def run(name, *arguments, capture=False):
        executable = shutil.which(name)
        if not executable:
            raise RuntimeError(f'Missing build tool: {name}')
        return subprocess.run([executable, *map(str, arguments)], check=True, close_fds=False,
                              env=environment, text=True, capture_output=capture)
    with tempfile.TemporaryDirectory(prefix='melty-pycuda-') as directory:
        work = Path(directory)
        source = work / 'source'
        python = work / 'build-env/bin/python'
        run('git', 'clone', '--no-hardlinks', args.source_repo, source)
        run('git', '-C', source, 'checkout', '--detach', revision)
        run('git', '-C', source, 'submodule', 'update', '--init', '--recursive')
        setup = source / 'setup.py'
        text = setup.read_text()
        assert 'Switch("CUDA_ENABLE_GL", False,' in text
        setup.write_text(text.replace('Switch("CUDA_ENABLE_GL", False,', 'Switch("CUDA_ENABLE_GL", True,'))
        version = source / 'pycuda/__init__.py'
        version.write_text(version.read_text().replace('VERSION_STATUS = ""', 'VERSION_STATUS = "+melty.gl1"'))
        helper = source / 'aksetup_helper.py'
        helper.write_text(helper.read_text().replace('result = self.get_default_config_with_files()',
                                                      'result = self.get_default_config()'))
        build = source / 'pyproject.toml'
        text = build.read_text().replace('"setuptools",', '"setuptools==80.9.0",').replace('"wheel",', '"wheel==0.45.1",').replace('"numpy>=1.24",', '"numpy==2.2.6",')
        build.write_text(text)
        run('uv', 'venv', '--python', args.python, work / 'build-env')
        raw = output / 'raw'
        raw.mkdir(exist_ok=True)
        run('uv', 'build', '--python', python, '--out-dir', raw, source)
        for archive in raw.glob('*.tar.gz'):
            shutil.copy2(archive, output / archive.name)
        run('uv', 'pip', 'install', '--python', python, 'auditwheel==6.4.2', 'patchelf==0.17.2.4')
        environment['PATH'] = str(python.parent) + os.pathsep + environment['PATH']
        environment['LD_LIBRARY_PATH'] = str(toolkit / 'lib64')
        for wheel in raw.glob('*.whl'):
            run(str(python.parent / 'auditwheel'), 'repair', '--exclude', 'libcuda.so.1', '--wheel-dir', output, wheel)
        repaired = next(output.glob('*.whl'))
        audit = run(str(python.parent / 'auditwheel'), 'show', repaired, capture=True)
        (output / 'auditwheel.txt').write_text(audit.stdout)

        artifacts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in output.iterdir() if path.suffix in ('.whl', '.gz')}
        manifest = {'upstream': 'https://github.com/inducer/pycuda', 'revision': revision,
                    'submodules': run('git', '-C', source, 'submodule', 'status', capture=True).stdout.splitlines(),
                    'variant': '2026.1+melty.gl1', 'opengl': True, 'curand': True,
                    'bundled': ['libcurand'], 'external': ['NVIDIA libcuda.so.1', 'platform C/C++ runtime'],
                    'build_dependencies': ['setuptools==80.9.0', 'wheel==0.45.1', 'numpy==2.2.6'],
                    'repair_tools': ['auditwheel==6.4.2', 'patchelf==0.17.2.4'],
                    'python': run(str(python), '--version', capture=True).stdout.strip(),
                    'platform': platform.platform(), 'toolkit': str(toolkit),
                    'nvcc': run(str(toolkit / 'bin/nvcc'), '--version', capture=True).stdout.strip(),
                    'compiler': run('g++', '--version', capture=True).stdout.splitlines()[0],
                    'uv': run('uv', '--version', capture=True).stdout.strip(), 'sha256': artifacts}
        (output / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
