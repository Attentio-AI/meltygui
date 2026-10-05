#!/usr/bin/env python3
"""Assemble an application's locked CPython 3.13 iPhone package set.

Run with build/dependencies/tools/bin/python after the native build recipes.
Downloads are hash pinned in dependencies.json. Local packages are built as
ordinary wheels; no desktop virtualenv, editable installs or .pth files ship.
"""
from __future__ import annotations

import argparse
import email
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from meltygui.platforms.ios.prepare_bundle import validate_device_binary

from meltygui.platforms.ios import build_directory

BUILD = build_directory()
ROOT = Path(__file__).resolve().parent


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(args, *, env=None):
    subprocess.run(list(map(str, args)), check=True, close_fds=False, env=env)


def download(pin, output, offline):
    path = output / pin['url'].rsplit('/', 1)[-1]
    if not path.exists():
        if offline:
            raise FileNotFoundError(f'Missing offline input: {path}')
        temporary = path.with_suffix('.download')
        try:
            with urllib.request.urlopen(pin['url'], timeout=60) as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target)
            if digest(temporary) != pin['sha256']:
                raise ValueError(f'Wrong download hash: {path.name}')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(path) != pin['sha256']:
        raise ValueError(f'Wrong cached hash: {path}')
    return path


def pure_wheel(pin, archive, output):
    if pin['kind'] == 'wheel':
        return archive
    wheels = output / 'wheels'
    wheels.mkdir(exist_ok=True)
    candidates = list(wheels.glob(pin['name'].replace('-', '_') + '-*.whl'))
    if candidates:
        wheel, = candidates
        return wheel
    with tarfile.open(archive) as source:
        source.extractall(output, filter='data')
        directory = output / PurePosixPath(source.getnames()[0]).parts[0]
    env = dict(os.environ)
    if pin['name'] == 'pyyaml-ft':
        # Upstream supports the same YAML API without its optional C speedup.
        env['PYYAML_FORCE_LIBYAML'] = '0'
    elif pin['name'] == 'watchdog':
        # Its upstream polling observer is selected automatically on iOS.
        setup = directory / 'setup.py'
        old = 'is_macos = sys.platform == "darwin" and not machine().lower().startswith(_apple_devices)'
        contents = setup.read_text()
        if old not in contents:
            raise ValueError('Pinned Watchdog build configuration changed')
        setup.write_text(contents.replace(old, 'is_macos = False  # iOS: no FSEvents extension'))
    else:
        raise ValueError(f'No portable build recipe for {pin["name"]}')
    runner = "import os,runpy,sys; os.chdir(sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path('setup.py',run_name='__main__')"
    run([sys.executable, '-c', runner, directory, 'setup.py', 'bdist_wheel', '--dist-dir', wheels], env=env)
    wheel, = wheels.glob(pin['name'].replace('-', '_') + '-*.whl')
    return wheel


def install_wheel(wheel, destination):
    """Install resources plus metadata, refusing code from another OS/CPU."""
    with zipfile.ZipFile(wheel) as archive:
        metadata_path, = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        metadata = email.message_from_bytes(archive.read(metadata_path))
        for member in archive.infolist():
            if member.is_dir():
                continue
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or '..' in relative.parts or '\\' in member.filename or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError(f'Unsafe wheel path: {member.filename}')
            if relative.parts[0].endswith('.data'):
                if relative.parts[1] not in ('purelib', 'platlib'):
                    continue  # Installed command scripts and build headers are not app resources.
                relative = PurePosixPath(*relative.parts[2:])
            if relative.suffix in ('.pth', '.dylib', '.dll', '.pyd'):
                raise ValueError(f'Unsupported wheel resource: {member.filename}')
            target = destination / relative
            if target.exists():
                raise ValueError(f'Conflicting package resource: {relative}')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
            if target.suffix == '.so':
                validate_device_binary(target)
    return dict(name=metadata['Name'], version=metadata['Version'], wheel=str(wheel), sha256=digest(wheel))


def device_environment():
    return default_environment() | dict(sys_platform='ios', platform_system='iOS', platform_machine='arm64',
                                               platform_release='17.0', platform_version='17.0', os_name='posix',
                                               implementation_name='cpython', platform_python_implementation='CPython',
                                               python_version='3.13', python_full_version='3.13.14',
                                               implementation_version='3.13.14', extra='')


def validate_dependencies(packages):
    installed = {canonicalize_name(d.metadata['Name']): d for d in distributions(path=[str(packages)])}
    environment = device_environment()
    for name, distribution in installed.items():
        supported = distribution.metadata.get('Requires-Python')
        if supported and not SpecifierSet(supported).contains(environment['python_full_version']):
            raise ValueError(f'{name} requires Python {supported}; bundled runtime is {environment["python_full_version"]}')
    pending = [(name, '') for name in installed]
    seen = set()
    while pending:
        name, extra = pending.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        for text in installed[name].requires or ():
            requirement = Requirement(text)
            if requirement.marker and not requirement.marker.evaluate(environment | {'extra': extra}):
                continue
            key = canonicalize_name(requirement.name)
            if key not in installed or not requirement.specifier.contains(installed[key].version):
                raise ValueError(f'{name} requires {requirement}; compatible dependency absent from bundle')
            pending.extend((key, value) for value in requirement.extras)
    return installed


def wheel_metadata(path):
    with zipfile.ZipFile(path) as archive:
        name, = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        return email.message_from_bytes(archive.read(name))


def resolve_wheels(requirements, wheels, pins, cache, offline=False):
    """Select the app's dependency closure from device wheels and pinned recipes.

    This is a locked build: incompatible constraints are an error, not a request
    to silently change package versions or install from the build host's venv.
    Unused recipes are neither downloaded nor shipped.
    """
    available = {}
    for path in wheels:
        metadata = wheel_metadata(path)
        name = canonicalize_name(metadata['Name'])
        if name in available:
            raise ValueError(f'Multiple device wheels supplied for {name}')
        available[name] = Path(path), metadata
    pins = {canonicalize_name(pin['name']): pin for pin in pins}
    pending = [(Requirement(value), '') for value in requirements]
    selected, inspected = {}, set()
    environment = device_environment()
    while pending:
        requirement, parent_extra = pending.pop()
        if requirement.marker and not requirement.marker.evaluate(environment | {'extra': parent_extra}):
            continue
        name = canonicalize_name(requirement.name)
        if requirement.url:
            raise ValueError(f'Supply a built device wheel for direct dependency {requirement}')
        if name not in available:
            if name not in pins:
                raise ValueError(f'No device wheel or pinned recipe for {requirement}')
            pin = pins[name]
            wheel = pure_wheel(pin, download(pin, cache, offline), cache)
            available[name] = wheel, wheel_metadata(wheel)
        wheel, metadata = available[name]
        if not requirement.specifier.contains(metadata['Version']):
            raise ValueError(f'{requirement} conflicts with supplied {name} {metadata["Version"]}')
        selected[name] = wheel
        for extra in {'', *requirement.extras}:
            if (name, extra) in inspected:
                continue
            inspected.add((name, extra))
            pending.extend((Requirement(value), extra) for value in metadata.get_all('Requires-Dist', []))
    return list(selected.values())


def copy_application(config, destination):
    """Stage declared Python sources and resources, independent of app schema."""
    root = config['root']
    destination = Path(destination).resolve()
    if destination == root or destination in root.parents:
        raise ValueError('Application staging must not replace its source tree')
    destination.mkdir(parents=True, exist_ok=True)
    ignore_common = shutil.ignore_patterns('.git', '__pycache__', '*.pyc')

    def ignore(directory, names):
        excluded = ignore_common(directory, names)
        if Path(directory) == root:
            excluded.update({'.venv', 'venv', 'build', 'dist'}.intersection(names))
        return excluded
    for pattern in [*config['sources'], *config['resources']]:
        relative = PurePosixPath(pattern)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError(f'Application resource must be relative: {pattern}')
        matches = [root] if pattern == '.' else list(root.glob(pattern))
        if not matches:
            raise ValueError(f'Application resource does not exist: {pattern}')
        for source in matches:
            if not source.resolve().is_relative_to(root):
                raise ValueError(f'Application resource escapes its root: {source}')
            if source == destination or source in destination.parents:
                raise ValueError('Application staging cannot include its output directory')
            target = destination / source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True, ignore=ignore)
            else:
                shutil.copy2(source, target)
    entry = destination.joinpath(*config['entry_module'].split('.'))
    if not entry.with_suffix('.py').is_file() and not (entry / '__init__.py').is_file():
        raise ValueError(f'Application sources do not include entry {config["entry_module"]}')


def main():
    from meltygui.platforms.ios.application import read_application
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, default=BUILD / 'app-bundle')
    parser.add_argument('--wheel-dir', type=Path, action='append', default=[])
    parser.add_argument('--package-source', type=Path, action='append', default=[])
    parser.add_argument('--lock', type=Path, default=ROOT / 'dependencies.json')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    application = read_application(args.application)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    pure = BUILD / 'dependencies/pure'
    pure.mkdir(parents=True, exist_ok=True)
    wheels = []
    local = output / 'wheels'
    local.mkdir(exist_ok=True)
    for source in args.package_source:
        with tempfile.TemporaryDirectory(prefix='build-', dir=local) as temporary:
            run([sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', temporary, source])
            wheel, = Path(temporary).glob('*.whl')
            destination = local / wheel.name
            shutil.copy2(wheel, destination)
            wheels.append(destination)
    directories = args.wheel_dir or [BUILD / 'binding', BUILD / 'dependencies/numeric/wheelhouse',
                                    BUILD / 'dependencies/platform/wheels', BUILD / 'dependencies/rust/wheelhouse',
                                    BUILD / 'dependencies/crypto/wheelhouse']
    for directory in directories:
        files = sorted(directory.glob('*.whl'))
        wheels.extend(files)
    wheels = resolve_wheels(application['dependencies'], wheels,
                            json.loads(args.lock.read_text())['packages'], pure, args.offline)
    with tempfile.TemporaryDirectory(prefix='staging-', dir=output) as temporary:
        packages = Path(temporary) / 'packages'
        packages.mkdir()
        manifest = [install_wheel(wheel, packages) for wheel in wheels]
        installed = validate_dependencies(packages)
        app = Path(temporary) / 'app'
        copy_application(application, app)
        for name in ('app', 'packages'):
            destination = output / name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(str(Path(temporary) / name), destination)
    (output / 'manifest.json').write_text(json.dumps({'schema': 1, 'packages': manifest}, indent=2) + '\n')
    print(f'Staged {len(installed)} distributions; complete iOS dependency closure verified: {output}')


if __name__ == '__main__':
    main()
