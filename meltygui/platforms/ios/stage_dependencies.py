#!/usr/bin/env python3
"""Assemble an application's locked iPhone package set for its project Python.

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
from packaging.utils import canonicalize_name, parse_wheel_filename
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


def install_wheel(wheel, destination, *, python_version=None):
    """Install resources plus metadata, refusing code from another OS/CPU."""
    if python_version and not compatible_wheel(wheel, python_version):
        raise ValueError(f'Wheel {wheel} does not match iOS project Python {python_version}')
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


def device_environment(python_version=None):
    full_version = python_version or '.'.join(map(str, sys.version_info[:3]))
    version = '.'.join(full_version.split('.')[:2])
    return default_environment() | dict(sys_platform='ios', platform_system='iOS', platform_machine='arm64',
                                               platform_release='17.0', platform_version='17.0', os_name='posix',
                                               implementation_name='cpython', platform_python_implementation='CPython',
                                               python_version=version, python_full_version=full_version,
                                               implementation_version=full_version, extra='')


def validate_dependencies(packages, *, python_version=None):
    installed = {canonicalize_name(d.metadata['Name']): d for d in distributions(path=[str(packages)])}
    environment = device_environment(python_version)
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


def compatible_wheel(path, python_version):
    major, minor = map(int, python_version.split('.')[:2])
    tag_version = f'{major}{minor}'
    _, _, _, tags = parse_wheel_filename(Path(path).name)
    for tag in tags:
        if tag.platform != 'any' and not (tag.platform.startswith('ios_') and tag.platform.endswith('_arm64_iphoneos')):
            continue
        if tag.interpreter in (f'py{major}', f'py{tag_version}', f'cp{tag_version}') and tag.abi in ('none', 'abi3', f'cp{tag_version}'):
            return True
        if tag.abi == 'abi3' and tag.interpreter.startswith(f'cp{major}'):
            minimum = tag.interpreter[len(f'cp{major}'):]
            if minimum.isdigit() and int(minimum) <= minor:
                return True
    return False


def resolve_wheels(requirements, wheels, pins, cache, offline=False, *, python_version=None, acquire_wheel=None):
    """Select the app's dependency closure from device wheels and pinned recipes.

    Explicit wheels and manual staging keep their locked versions. Automatic
    preparation may acquire a wheel when a catalogue pin cannot satisfy the
    target Python or requirement. Never copy the build host's installed packages.
    """
    available = {}
    environment = device_environment(python_version)
    for path in wheels:
        if not compatible_wheel(path, environment['python_full_version']):
            continue
        metadata = wheel_metadata(path)
        name = canonicalize_name(metadata['Name'])
        if name in available:
            raise ValueError(f'Multiple device wheels supplied for {name}')
        available[name] = Path(path), metadata
    pins = {canonicalize_name(pin['name']): pin for pin in pins}
    pending = [(Requirement(value), '') for value in requirements]
    selected, inspected = {}, set()
    while pending:
        requirement, parent_extra = pending.pop()
        if requirement.marker and not requirement.marker.evaluate(environment | {'extra': parent_extra}):
            continue
        name = canonicalize_name(requirement.name)
        if requirement.url:
            raise ValueError(f'Supply a built device wheel for direct dependency {requirement}')
        if name not in available:
            if name not in pins:
                if acquire_wheel is None:
                    raise ValueError(f'No device wheel or pinned recipe for {requirement}')
                wheel = acquire_wheel(requirement)
            else:
                pin = pins[name]
                wheel = pure_wheel(pin, download(pin, cache, offline), cache)
                metadata = wheel_metadata(wheel)
                if acquire_wheel is not None and (
                    not requirement.specifier.contains(metadata['Version']) or
                    not SpecifierSet(metadata.get('Requires-Python') or '').contains(environment['python_full_version'])
                ):
                    wheel = acquire_wheel(requirement)
            metadata = wheel_metadata(wheel)
            if canonicalize_name(metadata['Name']) != name:
                raise ValueError(f'Expected {name}, acquired {metadata["Name"]}')
            available[name] = wheel, metadata
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


def source_metadata_stamp(source):
    """Dependency edits in local packages invalidate their staged wheel metadata."""
    result = {}
    for name in ('pyproject.toml', 'setup.cfg', 'setup.py'):
        path = Path(source) / name
        if path.is_file():
            stat = path.stat()
            result[name] = [stat.st_mtime_ns, stat.st_size]
    return result


def refresh_local_packages(packages, *, python_version=None):
    """Rebuild explicitly staged source wheels before a device run.

    Reinstall the recorded device wheels as a set; never copy a desktop venv
    or leave deleted source files behind. Legacy manifests have no sources.
    """
    packages = Path(packages)
    manifest_path = packages.parent / 'manifest.json'
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    python_version = python_version or manifest.get('python_full_version')
    if not any(item.get('source') for item in manifest['packages']):
        return
    from meltygui.platforms.ios.devices import input_stamps
    source_inputs = {item['name']: input_stamps([item['source']])
                     for item in manifest['packages'] if item.get('source')}
    changed = {item['name'] for item in manifest['packages'] if item.get('source')
               and item.get('source_inputs') != source_inputs[item['name']]}
    if not changed:
        return
    with tempfile.TemporaryDirectory(prefix='refresh-', dir=packages.parent) as temporary:
        temporary = Path(temporary)
        staged = temporary / 'packages'
        staged.mkdir()
        refreshed = []
        built = []
        for item in manifest['packages']:
            source = item.get('source')
            if source and item['name'] in changed:
                source_stamp = source_metadata_stamp(source)
                output = temporary / canonicalize_name(item['name'])
                run([sys.executable, '-m', 'build', '--wheel', '--outdir', output, source])
                wheel, = output.glob('*.whl')
            else:
                wheel = Path(item['wheel'])
                if digest(wheel) != item['sha256']:
                    raise ValueError(f'Staged device wheel changed: {wheel}')
            entry = install_wheel(wheel, staged, python_version=python_version)
            if source and item['name'] in changed:
                destination = packages.parent / 'wheels' / wheel.name
                built.append((wheel, destination))
                entry.update(wheel=str(destination), source=source, source_metadata=source_stamp)
            elif source:
                entry.update(source=source, source_metadata=item.get('source_metadata'))
            if source:
                entry['source_inputs'] = source_inputs[item['name']]
            refreshed.append(entry)
        validate_dependencies(staged, python_version=python_version)
        for wheel, destination in built:
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(wheel, destination)
        # All builds and validation completed before replacing the runnable set.
        shutil.rmtree(packages)
        shutil.move(str(staged), packages)
        manifest['packages'] = refreshed
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


def main():
    from meltygui.platforms.ios.application import read_application
    from meltygui.platforms.ios.runtime import project_python, python_info
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', type=Path, default=Path.cwd())
    parser.add_argument('--project-python', type=Path, help='Project venv interpreter (default: APPLICATION/.venv/bin/python)')
    parser.add_argument('--output', type=Path, default=BUILD / 'app-bundle')
    parser.add_argument('--wheel-dir', type=Path, action='append', default=[])
    parser.add_argument('--package-source', type=Path, action='append', default=[])
    parser.add_argument('--lock', type=Path, default=ROOT / 'dependencies.json')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    application = read_application(args.application)
    runtime = python_info(project_python(application['root'], args.project_python))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    pure = BUILD / 'dependencies/pure'
    pure.mkdir(parents=True, exist_ok=True)
    wheels = []
    local = output / 'wheels'
    local.mkdir(exist_ok=True)
    package_sources = {}
    for source in args.package_source:
        with tempfile.TemporaryDirectory(prefix='build-', dir=local) as temporary:
            run([sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', temporary, source])
            wheel, = Path(temporary).glob('*.whl')
            destination = local / wheel.name
            shutil.copy2(wheel, destination)
            wheels.append(destination)
            package_sources[canonicalize_name(wheel_metadata(destination)["Name"])] = str(source.resolve())
    directories = args.wheel_dir or [BUILD / 'binding', BUILD / 'dependencies/numeric/wheelhouse',
                                    BUILD / 'dependencies/platform/wheels', BUILD / 'dependencies/rust/wheelhouse',
                                    BUILD / 'dependencies/crypto/wheelhouse']
    for directory in directories:
        files = sorted(directory.glob('*.whl'))
        wheels.extend(files)
    wheels = resolve_wheels(application['dependencies'], wheels,
                            json.loads(args.lock.read_text())['packages'], pure, args.offline,
                            python_version=runtime['full_version'])
    with tempfile.TemporaryDirectory(prefix='staging-', dir=output) as temporary:
        packages = Path(temporary) / 'packages'
        packages.mkdir()
        manifest = [install_wheel(wheel, packages, python_version=runtime['full_version']) for wheel in wheels]
        for item in manifest:
            source = package_sources.get(canonicalize_name(item['name']))
            if source:
                item['source'] = source
        installed = validate_dependencies(packages, python_version=runtime['full_version'])
        app = Path(temporary) / 'app'
        copy_application(application, app)
        for name in ('app', 'packages'):
            destination = output / name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(str(Path(temporary) / name), destination)
    (output / 'manifest.json').write_text(json.dumps({'schema': 1, 'packages': manifest,
        'python_full_version': runtime['full_version']}, indent=2) + '\n')
    print(f'Staged {len(installed)} distributions; complete iOS dependency closure verified: {output}')


if __name__ == '__main__':
    main()
