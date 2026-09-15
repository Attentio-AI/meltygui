"""Prepare auditable, namespaced native binding sources for wheel/sdist builds."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import urllib.request

IMGUI_URL = 'https://files.pythonhosted.org/packages/f1/aa/4abb0d3d6054da9a4390160fc25ca743a824263a9931cc6a95f30e3d75b4/imgui-2.0.0.tar.gz'
IMGUI_SHA256 = '2fbdb8eed3b8dbd7ea98af9e4c1c6582b0bc4da942a258de16333d8c653d67e1'
PYCUDA_REVISION = '19bed9035edb9990b2928e297125d8e9d470130b'


def prepare(kind, destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise SystemExit(f'Refusing to overwrite {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if kind == 'imgui':
        archive = destination.parent / 'imgui-2.0.0.tar.gz'
        with urllib.request.urlopen(IMGUI_URL) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != IMGUI_SHA256:
            raise SystemExit('Upstream source checksum mismatch')
        archive.write_bytes(data)
        with tarfile.open(archive) as source:
            source.extractall(destination.parent, filter='data')
        (destination.parent / 'imgui-2.0.0').rename(destination)
        for name in ('core', 'internal'):
            (destination / 'imgui' / f'{name}.cpp').unlink(missing_ok=True)
        version = '2.0.0.post1'
        provenance = {'url': IMGUI_URL, 'sha256': IMGUI_SHA256}
    else:
        subprocess.run(['git', 'clone', '--quiet', 'https://github.com/inducer/pycuda.git', str(destination)], check=True)
        subprocess.run(['git', '-C', str(destination), 'checkout', '--quiet', PYCUDA_REVISION], check=True)
        subprocess.run(['git', '-C', str(destination), 'submodule', 'update', '--init', '--recursive', '--quiet'], check=True)
        version = '2026.1.post1'
        provenance = {'url': 'https://github.com/inducer/pycuda', 'revision': PYCUDA_REVISION}
        setup = destination / 'setup.py'
        source = setup.read_text()
        assert 'Switch("CUDA_ENABLE_GL", False,' in source
        setup.write_text(source.replace('Switch("CUDA_ENABLE_GL", False,', 'Switch("CUDA_ENABLE_GL", True,'))
        helper = destination / 'aksetup_helper.py'
        helper.write_text(helper.read_text().replace('result = self.get_default_config_with_files()',
                                                   'result = self.get_default_config()'))
    namespace = 'meltygui_' + kind
    # Cython's generated class names and absolute Python imports must agree.
    # Keep upstream C/C++ libraries and header paths unchanged.
    for path in destination.rglob('*'):
        if not path.is_file() or '.git' in path.parts:
            continue
        if path.suffix not in {'.py', '.pyx', '.pxd', '.pxi', '.in', '.cfg', '.toml'}:
            continue
        source = path.read_text()
        source = re.sub(r'\b' + kind + r'\b', namespace, source)
        if kind == 'imgui':
            source = source.replace(namespace + '-cpp', 'imgui-cpp').replace(namespace + '.h', 'imgui.h').replace('imgui-cpp/' + namespace, 'imgui-cpp/imgui')
        path.write_text(source)
    (destination / kind).rename(destination / namespace)
    setup = destination / 'setup.py'
    source = setup.read_text()
    source = re.sub(r'name\s*=\s*[\'"]' + namespace + r'[\'"]', f'name="meltygui-{kind}"', source)
    source = source.replace('EXTRA_DEFINES["PYGPU_PACKAGE"] = "meltygui_pycuda"', 'EXTRA_DEFINES["PYGPU_PACKAGE"] = "pycuda"')
    source = source.replace('set_up_shipped_boost_if_requested("meltygui_pycuda"', 'set_up_shipped_boost_if_requested("pycuda"')
    source = source.replace('version=ver_dic["VERSION_TEXT"]', f'version="{version}"')
    source = source.replace('python_requires="~=3.8"', 'python_requires=">=3.12,<3.13"')
    source = source.replace('https://github.com/inducer/meltygui_pycuda', 'https://github.com/inducer/pycuda')
    source = re.sub(r'version\s*=\s*VERSION(?:_TEXT)?\b', f'version="{version}"', source)
    source = source.replace('packages=find_packages(', f'python_requires=">=3.12,<3.13",\n    packages=find_packages(')
    source = re.sub(r'url\s*=\s*[\'"][^\'"]+[\'"]', 'url="https://github.com/valine/meltygui"', source)
    # Upstream authorship and license files are retained; this is a maintained build variant.
    source = source.replace('long_description=read(README)', 'long_description=read("MELTYGUI.md")')
    source = source.replace('long_description=open("README.rst").read(),', 'long_description=open("MELTYGUI.md").read(),\n        long_description_content_type="text/markdown",')
    setup.write_text(source)
    build = destination / 'pyproject.toml'
    if kind == 'imgui':
        source = build.read_text().split('[build-system]')[0]
        build.write_text(source + '''[build-system]
requires = ["Cython==0.29.37", "PyOpenGL==3.1.10", "glfw==2.10.0",
            "wheel==0.45.1", "click==8.1.8", "setuptools==80.9.0"]
build-backend = "setuptools.build_meta"
''')
    else:
        source = build.read_text().replace('"setuptools",', '"setuptools==80.9.0",').replace('"wheel",', '"wheel==0.45.1",').replace('"numpy>=1.24",', '"numpy==2.2.6",')
        build.write_text(source)
    changes = f'''# MeltyGUI {kind} binding\n\nNamespaced build of upstream {kind}, version {version}.\n\nImport as `{namespace}`. This package does not install or replace `{kind}`.\nOriginal authorship and licenses apply. MeltyGUI changes: private Python namespace,\npinned build dependencies, Python 3.12 support, and OpenGL enabled for PyCUDA.\nA source distribution is provided for local compilation when no wheel matches.\n'''
    (destination / 'MELTYGUI.md').write_text(changes)
    notice_files = (['imgui-MIT.txt', 'imstb_rectpack.h.license.txt', 'imstb_textedit.h.license.txt', 'imstb_truetype.h.license.txt']
                    if kind == 'imgui' else ['boost.txt', 'NVIDIA-CUDA-12.1-EULA.txt'])
    notices = destination / 'LICENSES'
    notices.mkdir(exist_ok=True)
    for name in notice_files:
        shutil.copy2(Path(__file__).parent / 'licenses' / name, notices / name)
    source = setup.read_text().replace('include_package_data=True,', 'include_package_data=True,\n        license_files=["LICENSE", "LICENSES/*"],')
    setup.write_text(source)
    manifest = destination / 'MANIFEST.in'
    with manifest.open('a') as output:
        output.write('\ninclude MELTYGUI.md\ninclude UPSTREAM.json\nrecursive-include LICENSES *\n')
    (destination / 'UPSTREAM.json').write_text(json.dumps(provenance, indent=2) + '\n')
    print(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=['imgui', 'pycuda'])
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    prepare(args.kind, args.destination)
