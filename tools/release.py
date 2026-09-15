"""Collect and verify explicit release artifacts; never upload unrelated dist files."""
import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {'meltygui': 'meltygui/', 'meltygui-imgui': 'meltygui_imgui/', 'meltygui-pycuda': 'meltygui_pycuda/'}


def verify_public_member(name, data):
    """Keep separately licensed IDE components out of core artifacts."""
    parts = Path(name).parts
    assert not {'meltygui_pro', 'meltyprivate'} & set(parts), ('IDE package in core artifact', name)
    if 'meltygui' not in parts:
        return
    if name.endswith(('.py', '.json')):
        assert not any(n in data for n in (b'meltygui_pro', b'meltyprivate')), ('Reverse dependency in core artifact', name)
        for implementation in (b'class OpenFiles(', b'class GitProxy',
                               b'def draw_code_editor(', b'class EditorProjectState(',
                               b'def assemble_dependencies('):
            assert implementation not in data, ('Private implementation in public artifact', name)


def verify(directory, require_license=False):
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    if require_license and (not project.get('license') or not (ROOT / 'LICENSE').is_file()):
        raise SystemExit('Publication needs the owner-selected project license and LICENSE file.')
    found = set()
    versions = {'meltygui': project['version'], 'meltygui-imgui': '2.0.0.post1',
                'meltygui-pycuda': '2026.1.post1'}
    for path in sorted(directory.glob('*.whl')):
        with zipfile.ZipFile(path) as wheel:
            names = wheel.namelist()
            metadata = BytesParser().parsebytes(wheel.read(next(n for n in names if n.endswith('.dist-info/METADATA'))))
            name = metadata['Name'].replace('_', '-')
            assert name in PACKAGES, (path, name)
            assert name not in found, ('Duplicate release wheel', name)
            assert metadata['Version'] == versions[name], path
            found.add(name)
            expected = PACKAGES[name]
            assert any(n.startswith(expected) for n in names), path
            assert not any(n.startswith(('imgui/', 'pycuda/')) for n in names), path
            assert not any(n.endswith(('.pkl', '/key.json', '.pyc')) for n in names), path
            assert not any(part in {'.git', '.venv', '.idea', '__pycache__'} for n in names for part in Path(n).parts), path
            assert all(not n.startswith('/') and '..' not in Path(n).parts for n in names)
            if name == 'meltygui':
                for member in names:
                    verify_public_member(member, wheel.read(member))
                if require_license:
                    assert metadata['License-Expression'] == project['license'], 'Rebuild after setting the license'
                    assert any(n.endswith('.dist-info/licenses/LICENSE') for n in names), path
                deps = metadata.get_all('Requires-Dist', [])
                assert any(d.startswith('meltygui-imgui') for d in deps)
                assert not any(d.startswith(('imgui[', 'imgui>', 'pycuda>')) for d in deps)
                assert not any(n in d.replace('_', '-') for d in deps for n in ('meltygui-pro', 'meltyprivate'))
            else:
                assert 'cp312-cp312-manylinux' in path.name, path
                assert any('/licenses/' in n for n in names), path
                if name.endswith('pycuda'):
                    assert any('NVIDIA-CUDA-12.1-EULA' in n for n in names), path
            print(f'{name} {metadata["Version"]}: verified')
    assert found == set(PACKAGES), found
    for path in directory.glob('*.tar.gz'):
        with tarfile.open(path) as source:
            names = source.getnames()
            assert not any(part in {'.git', '.venv', '.idea', '__pycache__'} for n in names for part in Path(n).parts), path
            if path.name.startswith('meltygui-'):
                for member in source.getmembers():
                    if member.isfile():
                        verify_public_member(member.name, source.extractfile(member).read())
    assert len(list(directory.glob('*.tar.gz'))) == 3
    artifacts = sorted([*directory.glob('*.whl'), *directory.glob('*.tar.gz')])
    receipt = {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in artifacts}
    (directory / 'SHA256.json').write_text(json.dumps(receipt, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['collect', 'verify'])
    parser.add_argument('--directory', type=Path, default=ROOT / 'dist/release')
    parser.add_argument('--require-license', action='store_true')
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.action == 'collect':
        for directory in [ROOT / 'dist', ROOT / 'dist/release-native/imgui', ROOT / 'dist/release-native/pycuda']:
            for pattern in ('*.whl', '*.tar.gz'):
                for file in directory.glob(pattern):
                    shutil.copy2(file, args.directory / file.name)
    verify(args.directory, args.require_license)


if __name__ == '__main__':
    main()
