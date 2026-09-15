"""Build imgui 2.0.0 for Python 3.12 after regenerating its stale Cython C++.

A local verification artifact; publishing under imgui requires upstream access.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('dist/support'))
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    uv = shutil.which('uv')
    if uv is None:
        raise SystemExit('uv must be on PATH')
    with urllib.request.urlopen('https://pypi.org/pypi/imgui/2.0.0/json') as response:
        release = json.load(response)
    source = next(item for item in release['urls'] if item['packagetype'] == 'sdist')
    with tempfile.TemporaryDirectory(prefix='meltygui-imgui-') as temporary:
        temporary = Path(temporary)
        archive = temporary / source['filename']
        with urllib.request.urlopen(source['url']) as response:
            archive.write_bytes(response.read())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != source['digests']['sha256']:
            raise SystemExit('imgui source checksum did not match PyPI metadata')
        with tarfile.open(archive) as bundle:
            bundle.extractall(temporary, filter='data')
        folder = temporary / 'imgui-2.0.0'
        for name in ('core', 'internal'):
            (folder / 'imgui' / f'{name}.cpp').unlink(missing_ok=True)
        project = folder / 'pyproject.toml'
        text = project.read_text()
        text = text[:text.index('[build-system]')] + '''[build-system]
requires = ["Cython==0.29.37", "PyOpenGL==3.1.10", "glfw==2.10.0",
            "wheel==0.45.1", "click==8.1.8", "setuptools==80.9.0"]
build-backend = "setuptools.build_meta"
'''
        project.write_text(text)
        subprocess.run([uv, 'build', '--python', '3.12', '--wheel', str(folder),
                        '--out-dir', str(args.output)], check=True, close_fds=False)
        (args.output / 'imgui-build.json').write_text(json.dumps({
            'source': source['url'], 'source_sha256': digest,
            'changes': ['Regenerate core.cpp and internal.cpp with Cython 0.29.37',
                        'Pin build dependencies'],
            'wheels': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in args.output.glob('imgui-*.whl')},
        }, indent=2) + '\n')


if __name__ == '__main__':
    main()
