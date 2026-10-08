"""Prepare and cache missing iOS runtimes/wheels on the build host, before Xcode."""
from __future__ import annotations

import argparse
from collections import deque
import fcntl
import hashlib
from importlib.metadata import distribution, PackageNotFoundError
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from urllib.parse import unquote, urlparse, urljoin, parse_qs
import zipfile

from packaging.specifiers import SpecifierSet
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from meltygui.platforms.ios import runtime
from meltygui.platforms.ios.prepare_bundle import validate_device_binary
from meltygui.platforms.ios import stage_dependencies as staging

ROOT = Path(__file__).resolve().parent
SUPPORT = 'https://api.github.com/repos/beeware/Python-Apple-support'
JPEG_VERSION = '3.1.4.1'
JPEG_SHA256 = 'ecae8008e2cc9ade2f2c1bb9d5e6d4fb73e7c433866a056bd82980741571a022'
LIBFFI_VERSION = '3.4.7-2'
LIBFFI_SHA256 = '4b20898346fb5b0875f30596d98a62d418acc225ad0a518fdf440d8496ec6b71'


def _json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'meltygui-ios-builder'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def _download(url, path, sha256=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        partial = path.with_suffix(path.suffix + '.download')
        try:
            with urllib.request.urlopen(url, timeout=60) as source, partial.open('wb') as target:
                shutil.copyfileobj(source, target)
            if sha256 and staging.digest(partial) != sha256:
                raise ValueError(f'Download checksum mismatch: {url}')
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    elif sha256 and staging.digest(path) != sha256:
        path.unlink()
        return _download(url, path, sha256)
    return path


def _run(command, *, env, log, directory=None):
    command = list(map(str, command))
    executable = shutil.which(command[0], path=env.get('PATH'))
    if executable is None:
        raise RuntimeError(f'Build tool not found: {command[0]}')
    command[0] = executable
    if directory is not None:
        # Chdir only in the new child, preserving the app's posix_spawn contract.
        command = [sys.executable, '-I', '-c',
                   'import os,sys; os.chdir(sys.argv[1]); os.execve(sys.argv[2],sys.argv[2:],os.environ)',
                   str(directory), *command]
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    print(f"iOS: {shlex.join(command[-8:])} (log: {log})", flush=True)
    with log.open('a') as output:
        start = output.tell()
        result = subprocess.run(command, env=env, close_fds=False, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        with log.open(errors='replace') as output:
            output.seek(start)
            tail, diagnostic = deque(maxlen=35), []
            for line in output:
                tail.append(line)
                if (not diagnostic and ('error:' in line.lower() or 'cmake error' in line.lower())) or 0 < len(diagnostic) < 6:
                    diagnostic.append(line)
        detail = ''.join(tail)
        first_error = ''.join(diagnostic)
        if first_error and first_error not in detail:
            detail = first_error + '\n[…]\n' + detail
        raise RuntimeError(f'iOS build failed (exit {result.returncode}); log: {log}\n{detail}')


def _thin_runtime(path):
    """Apple support archives wrap even a single device slice in a fat Mach-O."""
    data = path.read_bytes()
    formats = {b'\xca\xfe\xba\xbe': ('>', '5I'), b'\xbe\xba\xfe\xca': ('<', '5I'),
               b'\xca\xfe\xba\xbf': ('>', 'IIQQII'), b'\xbf\xba\xfe\xca': ('<', 'IIQQII')}
    if data[:4] not in formats:
        validate_device_binary(path)
        return
    endian, layout = formats[data[:4]]
    count, = struct.unpack_from(endian + 'I', data, 4)
    size = struct.calcsize(endian + layout)
    if 8 + count * size > len(data):
        raise ValueError(f'Truncated universal Python framework: {path}')
    for index in range(count):
        cpu, subtype, offset, length, *_ = struct.unpack_from(endian + layout, data, 8 + index * size)
        if cpu != 0x0100000C or subtype & 0xFFFFFF:
            continue
        if offset < 8 + count * size or offset + length > len(data):
            raise ValueError(f'Invalid ARM64 slice in {path}')
        temporary = path.with_suffix('.thin')
        try:
            temporary.write_bytes(data[offset:offset + length])
            temporary.chmod(path.stat().st_mode)
            validate_device_binary(temporary)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return
    raise ValueError(f'No ARM64 device slice in {path}')


def ensure_runtime(info, compiler, cache, env):
    """Use a release support package, or build BeeWare's backported iOS runtime."""
    version = info['version']
    output = cache / 'runtime'
    receipt = output / 'ready.json'
    if receipt.is_file():
        try:
            ready = json.loads(receipt.read_text())
            framework, library, platform_config = (output / ready[key] for key in ('framework', 'library', 'platform_config'))
            runtime.validate_runtime(info, framework, library)
            validate_device_binary(framework / 'Python')
            if (platform_config / 'make_cross_venv.py').is_file():
                return framework, library, platform_config
        except (OSError, ValueError, KeyError):
            pass
    output.mkdir(parents=True, exist_ok=True)
    print(f'iOS: preparing CPython {version} runtime…', flush=True)
    release = None
    for page in range(1, 6):
        releases = _json(f'{SUPPORT}/releases?per_page=100&page={page}')
        for item in releases:
            if item.get('draft') or item.get('prerelease'):
                continue
            release = next((asset for asset in item['assets']
                            if asset['name'].startswith(f'Python-{version}-iOS-support.')
                            and asset['name'].endswith('.tar.gz')), None)
            if release:
                break
        if release or len(releases) < 100:
            break
    if release:
        digest = release.get('digest') or ''
        archive = _download(release['browser_download_url'], output / release['name'],
                            digest.removeprefix('sha256:') if digest.startswith('sha256:') else None)
    else:
        source = output / 'source'
        if not (source / 'Makefile').is_file():
            _run(['git', 'clone', '--depth', '1', '--branch', version,
                  'https://github.com/beeware/Python-Apple-support.git', source],
                 env=env, log=output / 'runtime-build.log')
        _run(['/usr/bin/make', '-C', source, 'iOS', 'TARGETS-iOS=iphoneos.arm64',
              f'HOST_PYTHON={compiler}'], env=env, log=output / 'runtime-build.log')
        archive, = (source / 'dist').glob(f'Python-{version}-iOS-support.*.tar.gz')
    unpacked = output / 'support'
    if unpacked.exists():
        shutil.rmtree(unpacked)
    unpacked.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(unpacked, filter='data')
    xcframework = unpacked / 'Python.xcframework'
    device = xcframework / 'ios-arm64'
    framework = device / 'Python.framework'
    library = output / 'lib'
    stdlib = library / f'python{version}'
    if library.exists():
        shutil.rmtree(library)
    # Shared source + the device slice's extensions, never simulator modules.
    shutil.copytree(xcframework / 'lib' / f'python{version}', stdlib)
    shutil.copytree(device / 'lib-arm64' / f'python{version}', stdlib, dirs_exist_ok=True)
    runtime.validate_runtime(info, framework, library)
    _thin_runtime(framework / 'Python')
    for extension in stdlib.rglob('*.so'):
        _thin_runtime(extension)
    platform_config = device / 'platform-config/arm64-iphoneos'
    if not (platform_config / 'make_cross_venv.py').is_file():
        raise ValueError(f'Runtime support package is missing its cross-compiler configuration: {platform_config}')
    receipt.write_text(json.dumps({key: str(path.relative_to(output)) for key, path in
        (('framework', framework), ('library', library), ('platform_config', platform_config))}))
    return framework, library, platform_config


def _tools(cache, env):
    """Keep build dependencies out of both the user's venv and the application."""
    tools = cache / 'tools'
    python = tools / 'bin/python'
    if not python.is_file():
        _run([sys.executable, '-m', 'venv', tools], env=env, log=cache / 'tools.log')
    marker = tools / 'ready.json'
    signature = [(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in ROOT.glob('*.py')]
    signature = json.loads(json.dumps(sorted(signature)))
    try:
        ready = json.loads(marker.read_text()) == signature
    except (OSError, ValueError):
        ready = False
    if not ready:
        _run([python, '-m', 'pip', 'install', 'build', 'setuptools', 'setuptools-scm', 'wheel',
              'hatchling', 'packaging', 'Cython==3.2.4', 'cmake'], env=env, log=cache / 'tools.log')
        installed = distribution('meltygui')
        direct = json.loads(installed.read_text('direct_url.json') or '{}')
        url = direct.get('url', '')
        source = Path(unquote(urlparse(url).path)) if url.startswith('file:') else None
        requirement = str(source) if source and (source / 'pyproject.toml').is_file() else f'meltygui=={installed.version}'
        _run([python, '-m', 'pip', 'install', '--no-deps', '--force-reinstall', requirement],
             env=env, log=cache / 'tools.log')
        marker.write_text(json.dumps(signature))
    return python


def _rust(cache, env):
    cargo, rustup = cache / 'cargo', cache / 'rustup'
    env = dict(env, CARGO_HOME=str(cargo), RUSTUP_HOME=str(rustup))
    env['PATH'] = str(cargo / 'bin') + os.pathsep + env.get('PATH', '')
    ready = cache / 'rust-ready'
    if not ready.is_file() or not (cargo / 'bin/rustc').is_file():
        arch = 'aarch64' if platform.machine() == 'arm64' else 'x86_64'
        url = f'https://static.rust-lang.org/rustup/dist/{arch}-apple-darwin/rustup-init'
        with urllib.request.urlopen(url + '.sha256', timeout=60) as response:
            digest = response.read().decode().split()[0]
        installer = _download(url, cache / 'rustup-init', digest)
        installer.chmod(0o755)
        _run([installer, '-y', '--no-modify-path', '--profile', 'minimal', '--default-toolchain', 'stable',
              '--target', 'aarch64-apple-ios'], env=env, log=cache / 'rust.log')
        ready.touch()
    return env


def _validate_wheel(wheel, version):
    if not staging.compatible_wheel(wheel, version):
        raise ValueError(f'Builder produced a wheel for the wrong target: {wheel}')
    with tempfile.TemporaryDirectory(prefix='validate-', dir=wheel.parent) as temporary:
        staging.install_wheel(wheel, Path(temporary), python_version=version)
    return wheel


def _numpy_wheel(requirement, info, wheelhouse):
    """BeeWare publishes Accelerate-linked NumPy wheels outside PyPI."""
    index = 'https://pypi.anaconda.org/beeware/simple/numpy/'
    links = []
    class Links(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == 'a':
                link = dict(attrs).get('href')
                if link:
                    links.append(urljoin(index, link))
    try:
        with urllib.request.urlopen(index, timeout=60) as response:
            Links().feed(response.read().decode())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    candidates = []
    for link in links:
        filename = unquote(urlparse(link).path.rsplit('/', 1)[-1])
        if filename.endswith('.whl') and staging.compatible_wheel(filename, info['full_version']):
            _, version, _, _ = parse_wheel_filename(filename)
            if requirement.specifier.contains(version):
                candidates.append((version, filename, link))
    if not candidates:
        return None
    _, filename, link = max(candidates)
    digest = parse_qs(urlparse(link).fragment).get('sha256', [None])[0]
    return _validate_wheel(_download(link, wheelhouse / filename, digest), info['full_version'])


def _target_library_environment(env):
    """Keep native dependency discovery within the explicitly supplied target."""
    env = dict(env)
    # Pillow also consults environment paths and pkg-config before platform
    # guessing. Do not discover a Mac/Homebrew codec during a device build.
    for name in ('CFLAGS', 'CPPFLAGS', 'CXXFLAGS', 'LDFLAGS', 'CPATH', 'C_INCLUDE_PATH',
                 'CPLUS_INCLUDE_PATH', 'LIBRARY_PATH', 'LD_RUN_PATH', 'INCLUDE', 'LIB',
                 'AVIF_ROOT', 'FREETYPE_ROOT', 'HARFBUZZ_ROOT', 'FRIBIDI_ROOT',
                 'IMAGEQUANT_ROOT', 'JPEG2K_ROOT', 'LCMS_ROOT', 'RAQM_ROOT', 'TIFF_ROOT', 'WEBP_ROOT'):
        env.pop(name, None)
    env['PKG_CONFIG'] = '/usr/bin/false'
    return env


def _cffi_environment(source, cache, env):
    """Use BeeWare's iPhone libffi, including its iOS closure support."""
    env = _target_library_environment(env)
    prefix = cache / 'cffi-deps' / f'libffi-{LIBFFI_VERSION}'
    if not all((prefix / path).is_file() for path in
               ('lib/libffi.a', 'include/ffi.h', 'include/ffi_arm64.h', 'include/ffitarget_arm64.h')):
        archive = _download(
            f'https://github.com/beeware/cpython-apple-source-deps/releases/download/libFFI-{LIBFFI_VERSION}/libffi-{LIBFFI_VERSION}-iphoneos.arm64.tar.gz',
            cache / 'downloads' / f'libffi-{LIBFFI_VERSION}-iphoneos.arm64.tar.gz', LIBFFI_SHA256)
        prefix.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(prefix, filter='data')
    env.update(CFLAGS=shlex.join(['-I' + str(prefix / 'include')]),
               LDFLAGS=shlex.join(['-L' + str(prefix / 'lib')]))
    license = source / 'LICENSE'
    marker = f'\nBundled libffi {LIBFFI_VERSION}\n'
    text = license.read_text()
    if marker not in text:
        # The support archive carries the upstream MIT notice in its header.
        notice = (prefix / 'include/ffi_arm64.h').read_text().split('/*', 1)[1].split('*/', 1)[0]
        license.write_text(text + marker + notice + '\n')
    return env


def _pillow_environment(source, cache, env):
    """Supply Pillow's required JPEG/zlib libraries for the iPhone target."""
    env = _target_library_environment(env)
    sdk = subprocess.check_output(['/usr/bin/xcrun', '--sdk', 'iphoneos', '--show-sdk-path'],
                                  env=env, close_fds=False, text=True).strip()
    env['SDKROOT'] = sdk
    output = cache / 'pillow-deps' / f'libjpeg-turbo-{JPEG_VERSION}'
    prefix = output / 'install'
    artifacts = ('lib/libjpeg.a', 'include/jpeglib.h', 'include/jmorecfg.h',
                 'include/jconfig.h', 'include/jerror.h', 'LICENSE.md', 'README.ijg')
    if not (output / 'ready').is_file() or not all((prefix / path).is_file() for path in artifacts):
        print('iOS: building Pillow JPEG dependency for iPhone ARM64…', flush=True)
        archive = _download(
            f'https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/{JPEG_VERSION}/libjpeg-turbo-{JPEG_VERSION}.tar.gz',
            cache / 'downloads' / f'libjpeg-turbo-{JPEG_VERSION}.tar.gz', JPEG_SHA256)
        sources = output / 'source'
        sources.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(sources, filter='data')
        jpeg = sources / f'libjpeg-turbo-{JPEG_VERSION}'
        tools = _tools(cache, env)
        cmake = tools.parent / 'cmake'
        clang = subprocess.check_output(['/usr/bin/xcrun', '--sdk', 'iphoneos', '--find', 'clang'],
                                        env=env, close_fds=False, text=True).strip()
        native = output / 'native'
        _run([cmake, '--fresh', '-S', jpeg, '-B', native, '-G', 'Unix Makefiles',
              '-DCMAKE_SYSTEM_NAME=iOS', '-DCMAKE_SYSTEM_PROCESSOR=arm64', '-DCMAKE_OSX_ARCHITECTURES=arm64',
              f'-DCMAKE_OSX_SYSROOT={sdk}', '-DCMAKE_OSX_DEPLOYMENT_TARGET=17.0',
              f'-DCMAKE_C_COMPILER={clang}', '-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY',
              '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_POSITION_INDEPENDENT_CODE=ON',
              '-DCMAKE_C_VISIBILITY_PRESET=hidden', '-DENABLE_SHARED=OFF',
              '-DENABLE_STATIC=ON', '-DWITH_TURBOJPEG=OFF'], env=env, log=output / 'build.log')
        _run([cmake, '--build', native, '--target', 'jpeg-static', '--parallel', '2'],
             env=env, log=output / 'build.log')
        (prefix / 'include').mkdir(parents=True, exist_ok=True)
        (prefix / 'lib').mkdir(exist_ok=True)
        shutil.copy2(native / 'libjpeg.a', prefix / 'lib/libjpeg.a')
        shutil.copy2(native / 'jconfig.h', prefix / 'include/jconfig.h')
        for header in ('jpeglib.h', 'jmorecfg.h', 'jerror.h'):
            shutil.copy2(jpeg / 'src' / header, prefix / 'include' / header)
        for license in ('LICENSE.md', 'README.ijg'):
            shutil.copy2(jpeg / license, prefix / license)
        (output / 'ready').touch()
    # Pillow packages LICENSE in its wheel, including these statically linked
    # third-party notices. A retry must not append the notices twice.
    license = source / 'LICENSE'
    marker = f'\nBundled libjpeg-turbo {JPEG_VERSION}\n'
    text = license.read_text()
    if marker not in text:
        license.write_text(text + marker + (prefix / 'LICENSE.md').read_text() + '\n' +
                           (prefix / 'README.ijg').read_text())
    env.update(JPEG_ROOT=str(prefix), ZLIB_ROOT=str(Path(sdk) / 'usr'))
    return env


def _build_source(name, source, compiler, info, framework, platform_config, cache, env):
    """Compile a source distribution using the support package's cross-venv."""
    output = cache / 'builds' / name
    output.mkdir(parents=True, exist_ok=True)
    build_env = output / 'venv'
    python = build_env / 'bin/python'
    if not python.is_file():
        _run([compiler, '-m', 'venv', build_env], env=env, log=output / 'build.log')
    metadata = source / 'pyproject.toml'
    requirements = tomllib.loads(metadata.read_text()).get('build-system', {}).get('requires', []) if metadata.is_file() else ['setuptools', 'wheel']
    pth = build_env / f"lib/python{info['version']}/site-packages/_cross_venv.pth"
    # Install executable build tools for macOS, then turn only this owned venv
    # into a cross environment. Never alter the project's interpreter.
    pth.unlink(missing_ok=True)
    _run([python, '-m', 'pip', 'install', 'build', 'wheel', *requirements], env=env, log=output / 'build.log')
    _run([compiler, platform_config / 'make_cross_venv.py', build_env], env=env, log=output / 'build.log')
    build_env_vars = dict(env, IPHONEOS_DEPLOYMENT_TARGET='17.0',
                          PATH=os.pathsep.join((str(framework.parent / 'bin'), str(python.parent), env.get('PATH', ''))))
    sdk = subprocess.check_output(['/usr/bin/xcrun', '--sdk', 'iphoneos', '--show-sdk-path'],
                                  env=env, close_fds=False, text=True).strip()
    build_env_vars['SDKROOT'] = sdk
    build_env_vars.pop('MACOSX_DEPLOYMENT_TARGET', None)
    if name.startswith('cffi-'):
        build_env_vars = _cffi_environment(source, cache, build_env_vars)
    extra_file = output / 'backend-requirements.json'
    _run([python, '-c', 'import json,sys; from pathlib import Path; from build import ProjectBuilder; '
          'Path(sys.argv[2]).write_text(json.dumps(sorted(ProjectBuilder(sys.argv[1]).get_requires_for_build("wheel"))))',
          source, extra_file], env=build_env_vars, log=output / 'build.log')
    extra = json.loads(extra_file.read_text())
    if extra:
        pth.unlink(missing_ok=True)
        _run([python, '-m', 'pip', 'install', *extra], env=env, log=output / 'build.log')
        _run([compiler, platform_config / 'make_cross_venv.py', build_env], env=env, log=output / 'build.log')
        requirements.extend(extra)
    if any('maturin' in r or 'setuptools-rust' in r for r in requirements):
        build_env_vars = _rust(cache, build_env_vars)
        build_env_vars.pop('CARGO_ENCODED_RUSTFLAGS', None)
        clang = subprocess.check_output(['/usr/bin/xcrun', '--sdk', 'iphoneos', '--find', 'clang'],
                                        env=env, close_fds=False, text=True).strip()
        config = output / 'pyo3.cfg'
        config.write_text(f"implementation=CPython\nversion={info['version']}\nshared=true\n"
                          'pointer_width=64\nsuppress_build_script_link_lines=true\n'
                          f"ext_suffix=.cpython-{info['version'].replace('.', '')}-iphoneos.so\n")
        build_env_vars.update(PYO3_CONFIG_FILE=str(config), PYO3_CROSS='1',
                              PYO3_CROSS_PYTHON_VERSION=info['version'], CARGO_BUILD_TARGET='aarch64-apple-ios',
                              CARGO_TARGET_AARCH64_APPLE_IOS_LINKER=clang, CC_aarch64_apple_ios=clang,
                              CFLAGS_aarch64_apple_ios=shlex.join(['-target', 'arm64-apple-ios17.0', '-isysroot', sdk]),
                              RUSTFLAGS=shlex.join(['-C', f'link-arg=-F{framework.parent}',
                                                   '-C', 'link-arg=-framework', '-C', 'link-arg=Python',
                                                   '-C', 'link-arg=-isysroot', '-C', f'link-arg={sdk}']))
    settings = []
    if name.startswith('pillow-'):
        build_env_vars = _pillow_environment(source, cache, build_env_vars)
        # Keep both required codecs enabled; use only the supplied target paths.
        settings = ['-C', 'jpeg=enable', '-C', 'zlib=enable', '-C', 'platform-guessing=disable',
                    '-C', 'raqm=disable', '-C', 'imagequant=disable']
    wheels = output / 'wheels'
    if wheels.exists():
        shutil.rmtree(wheels)
    _run([python, '-m', 'build', '--wheel', '--no-isolation', *settings, '--outdir', wheels, source],
         env=build_env_vars, log=output / 'build.log')
    wheel, = wheels.glob('*.whl')
    return _validate_wheel(wheel, info['full_version'])


def acquire_wheel(requirement, compiler, info, framework, platform_config, cache, env, preferred=None):
    """Download an existing compatible wheel, otherwise build its source."""
    name = canonicalize_name(requirement.name)
    wheelhouse = cache / 'wheels'
    wheelhouse.mkdir(parents=True, exist_ok=True)
    if name in ('freetype-py', 'rtree'):
        library = Path(env.get('MELTY_IOS_TARGET_LIB', cache / 'runtime/lib'))
        ensure_platform(library, info, cache, env)
    for wheel in sorted(wheelhouse.glob('*.whl'), key=lambda p: parse_wheel_filename(p.name)[1], reverse=True):
        wheel_name, version, _, _ = parse_wheel_filename(wheel.name)
        if wheel_name == name and requirement.specifier.contains(version) and staging.compatible_wheel(wheel, info['full_version']):
            try:
                return _validate_wheel(wheel, info['full_version'])
            except (ValueError, OSError, zipfile.BadZipFile):
                wheel.unlink(missing_ok=True)
    if name == 'numpy':
        published = _numpy_wheel(requirement, info, wheelhouse)
        if published:
            return published
    if name in ('freetype-py', 'rtree'):
        library = cache / 'runtime/lib'
        # A reused external runtime can have its library outside this cache.
        library = Path(env.get('MELTY_IOS_TARGET_LIB', library))
        output = ensure_platform(library, info, cache, env)
        wheel, = (output / 'wheels').glob(name.replace('-', '_') + '-*.whl')
    elif name == 'meltygui-imgui':
        from meltygui.platforms.ios.build_imgui import VERSION
        if not requirement.specifier.contains(VERSION):
            raise ValueError(f'{requirement} does not match the ImGui version required by the Metal renderer ({VERSION})')
        tools = _tools(cache, env)
        output = cache / 'imgui'
        _run([tools, '-m', 'meltygui.platforms.ios.build_imgui', '--python-framework', framework,
              '--output', output, '--developer-dir', env['DEVELOPER_DIR']], env=env, log=cache / 'imgui.log')
        wheel, = output.glob('*.whl')
    else:
        metadata = _json(f'https://pypi.org/pypi/{name}/json')
        versions = sorted((Version(v) for v, files in metadata['releases'].items()
                           if files and not Version(v).is_prerelease and requirement.specifier.contains(v)), reverse=True)
        if preferred and Version(preferred) in versions:
            versions.remove(Version(preferred))
            versions.insert(0, Version(preferred))
        for version in versions:
            files = [f for f in metadata['releases'][str(version)] if not f.get('yanked') and
                     SpecifierSet(f.get('requires_python') or '').contains(info['full_version'])]
            candidates = [f for f in files if f['filename'].endswith('.whl') and
                          staging.compatible_wheel(f['filename'], info['full_version'])]
            if candidates:
                item = candidates[0]
                return _validate_wheel(_download(item['url'], wheelhouse / item['filename'],
                                                 item['digests']['sha256']), info['full_version'])
            sdists = [f for f in files if f['packagetype'] == 'sdist']
            if sdists:
                if name == 'cryptography':
                    from meltygui.platforms.ios.build_crypto import SOURCES
                    if str(version) == SOURCES['cryptography']['version']:
                        tools = _tools(cache, env)
                        crypto_env = _rust(cache, env)
                        output = cache / 'crypto'
                        _run([tools, '-m', 'meltygui.platforms.ios.build_crypto', '--output', output,
                              '--runtime', framework.parent, '--python-lib', env['MELTY_IOS_TARGET_LIB'],
                              '--project-python', compiler, '--developer-dir', env['DEVELOPER_DIR'],
                              '--cargo-home', crypto_env['CARGO_HOME'], '--rustup-home', crypto_env['RUSTUP_HOME']],
                             env=crypto_env, log=cache / 'crypto.log')
                        wheel, = (output / 'wheelhouse').glob('*.whl')
                        break
                item = sdists[0]
                archive = _download(item['url'], cache / 'downloads' / item['filename'], item['digests']['sha256'])
                source_root = cache / 'sources' / f'{name}-{version}'
                if source_root.exists():
                    shutil.rmtree(source_root)
                source_root.mkdir(parents=True)
                shutil.unpack_archive(archive, source_root, filter='data') if tarfile.is_tarfile(archive) else _unzip_source(archive, source_root)
                sources = [p for p in source_root.iterdir() if p.is_dir()]
                source = sources[0] if len(sources) == 1 else source_root
                wheel = _build_source(f'{name}-{version}', source, compiler, info, framework, platform_config, cache, env)
                break
        else:
            raise ValueError(f'Upstream has no source or wheel satisfying {requirement} for Python {info["full_version"]}')
    _validate_wheel(wheel, info['full_version'])
    destination = wheelhouse / wheel.name
    shutil.copy2(wheel, destination)
    return destination


def _unzip_source(archive, destination):
    with zipfile.ZipFile(archive) as source:
        for name in source.namelist():
            if not (destination / name).resolve().is_relative_to(destination.resolve()):
                raise ValueError(f'Source archive escapes its destination: {name}')
        source.extractall(destination)


def ensure_platform(library, info, cache, env):
    output = cache / 'platform'
    if all((output / f'frameworks/{name}.framework/{name}').is_file()
           for name in ('freetype', 'spatialindex_c')) and (output / 'manifest.json').is_file() and all(
               list((output / 'wheels').glob(name + '-*.whl')) for name in ('freetype_py', 'rtree')):
        return output
    tools = _tools(cache, env)
    recipe_env = dict(env, PATH=str(tools.parent) + os.pathsep + env.get('PATH', ''))
    _run([tools, '-m', 'meltygui.platforms.ios.build_platform_deps', '--output', output,
          '--python-stdlib', library / f"python{info['version']}", '--developer-dir', env['DEVELOPER_DIR']],
         env=recipe_env, log=cache / 'platform.log')
    return output


def _generation_options(build, config):
    """Preserve signing, extra renderer sources and frameworks on regeneration."""
    if config.get('generator'):
        return dict(config['generator'])
    objects = plistlib.loads((build / 'MeltyIOS.xcodeproj/project.pbxproj').read_bytes())['objects']
    settings = next((obj['buildSettings'] for obj in objects.values()
                     if obj.get('isa') == 'XCBuildConfiguration' and obj.get('buildSettings', {}).get('PRODUCT_BUNDLE_IDENTIFIER')), {})
    paths = [Path(obj['path']) for obj in objects.values() if obj.get('isa') == 'PBXFileReference'
             and Path(obj.get('path', '')).is_absolute()]
    return dict(team=settings.get('DEVELOPMENT_TEAM', ''), toolkit_dir=str(ROOT.parents[1]),
                renderer_sources=[str(p) for p in paths if p.suffix == '.mm' and p.parent != ROOT / 'Host'],
                embed_frameworks=[str(p) for p in paths if p.suffix == '.framework' and p.name != 'Python.framework'])


def _package_inputs(packages, info, requirements=()):
    if not packages or not Path(packages).is_dir():
        return False
    try:
        installed = staging.validate_dependencies(packages, python_version=info['full_version'])
        for item in installed.values():
            for filename in item.files or ():
                relative = Path(filename)
                if relative.parts and relative.parts[0].endswith('.data'):
                    if len(relative.parts) < 3 or relative.parts[1] not in ('purelib', 'platlib'):
                        continue
                    relative = Path(*relative.parts[2:])
                if relative.suffix in ('.so', '.py') and not (Path(packages) / relative).is_file():
                    return False
        environment = staging.device_environment(info['full_version'])
        for text in requirements:
            requirement = Requirement(text)
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            item = installed.get(canonicalize_name(requirement.name))
            if item is None or not requirement.specifier.contains(item.version):
                return False
        from packaging.tags import parse_tag
        for wheel_metadata in Path(packages).glob('*.dist-info/WHEEL'):
            for line in wheel_metadata.read_text().splitlines():
                if line.startswith('Tag: '):
                    tags = parse_tag(line.removeprefix('Tag: '))
                    if any(staging.compatible_wheel(f'package-1-{tag}.whl', info['full_version']) for tag in tags):
                        break
            else:
                return False
        abi = info['version'].replace('.', '')
        for path in Path(packages).rglob('*.so'):
            if '.cpython-' in path.name and f'.cpython-{abi}-' not in path.name:
                return False
    except (ValueError, OSError):
        return False
    return True


def _source_records(config):
    packages = config.get('packages_dir')
    manifest = Path(packages).parent / 'manifest.json' if packages else None
    return json.loads(manifest.read_text()).get('packages', []) if manifest and manifest.is_file() else []


def ensure_configuration(root, build, config, *, cache_root=None):
    """Repair missing/version-mismatched build inputs and update the Xcode project."""
    from meltygui.platforms.ios.application import read_application
    from meltygui.platforms.ios.devices import xcode_environment, input_stamps
    from meltygui.platforms.ios.generate import generate, generation_inputs

    compiler = runtime.configured_python(config | {'project_dir': str(root)})
    info = runtime.python_info(compiler)
    application = read_application(root)
    # No network, tool installation, or cross-build on an already prepared run.
    try:
        runtime.build_python(config | {'project_python': compiler})
        framework = config.get('python_framework')
        if framework is None:
            objects = plistlib.loads((build / 'MeltyIOS.xcodeproj/project.pbxproj').read_bytes())['objects']
            framework = next(obj['path'] for obj in objects.values()
                             if obj.get('isa') == 'PBXFileReference' and Path(obj.get('path', '')).name == 'Python.framework')
        validate_device_binary(Path(framework) / 'Python')
        ready = _package_inputs(config.get('packages_dir'), info, application['dependencies'])
        if ready:
            ready = all((Path(path) / Path(path).stem).is_file()
                        for path in _generation_options(build, config).get('embed_frameworks', []))
    except (ValueError, OSError, StopIteration):
        ready = False
    if ready:
        if config.get('generation_inputs') == generation_inputs():
            return config, compiler, info
        # Native host APIs/frameworks can change while Python inputs stay ready.
        # Regenerate locally so an existing project picks up those changes too.
        generate(application=root, app_dir=config['app_dir'], packages_dir=config['packages_dir'],
                 python_framework=framework, python_lib=config['python_lib'], project_python=compiler,
                 output=build, bundle_id=config['bundle_id'], entry_module=config['entry_module'],
                 **_generation_options(build, config))
        return json.loads((build / 'host-build.json').read_text()), compiler, info

    env = xcode_environment()
    if not env.get('DEVELOPER_DIR'):
        result = subprocess.run(['/usr/bin/xcode-select', '-p'], close_fds=False,
                                capture_output=True, text=True, check=True)
        env['DEVELOPER_DIR'] = result.stdout.strip()
    result = subprocess.run(['/usr/bin/xcrun', '--sdk', 'iphoneos', '--show-sdk-version'], env=env,
                            close_fds=False, capture_output=True, text=True, check=True)
    signature = dict(sdk=result.stdout.strip(), developer=env['DEVELOPER_DIR'],
                     magic=info['magic'], recipes=input_stamps([ROOT]))
    # Hash only small file-metadata records, not source contents for staleness.
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]
    base = Path(cache_root) if cache_root else Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'meltygui/ios'
    cache = base / f"cpython-{info['version']}" / f'iphoneos-arm64-{key}'
    cache.mkdir(parents=True, exist_ok=True)
    print(f"iOS: preparing Python {info['version']} build inputs; cache: {cache}", flush=True)
    with (cache / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        framework, library, platform_config = ensure_runtime(info, compiler, cache, env)
        env['MELTY_IOS_TARGET_LIB'] = str(library)
        records = _source_records(config)
        sources = {canonicalize_name(item['name']): item['source'] for item in records if item.get('source')}
        preferred = {canonicalize_name(item['name']): item['version'] for item in records}
        for name in ('meltygui', 'meltygui-pro'):
            if name not in sources:
                try:
                    direct = json.loads(distribution(name).read_text('direct_url.json') or '{}')
                    if direct.get('url', '').startswith('file:'):
                        source = Path(unquote(urlparse(direct['url']).path))
                        if (source / 'pyproject.toml').is_file():
                            sources[name] = str(source)
                except PackageNotFoundError:
                    pass
        tools = None

        def acquire(requirement):
            nonlocal tools
            name = canonicalize_name(requirement.name)
            if name in sources:
                tools = tools or _tools(cache, env)
                destination = build / 'ios-local-wheels' / name
                if destination.exists():
                    shutil.rmtree(destination)
                _run([tools, '-m', 'build', '--wheel', '--outdir', destination, sources[name]],
                     env=env, log=cache / f'{name}-local.log')
                wheel, = destination.glob('*.whl')
                return wheel
            return acquire_wheel(requirement, compiler, info, framework, platform_config, cache, env, preferred.get(name))

        pins = json.loads((ROOT / 'dependencies.json').read_text())['packages']
        pure = cache / 'pure'
        pure.mkdir(exist_ok=True)
        wheels = staging.resolve_wheels(application['dependencies'], [], pins, pure,
                                       python_version=info['full_version'], acquire_wheel=acquire)
        destination = build / 'ios-packages' / info['version']
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='packages-', dir=destination.parent) as temporary:
            staged = Path(temporary) / 'packages'
            staged.mkdir()
            entries = []
            for wheel in wheels:
                entry = staging.install_wheel(wheel, staged, python_version=info['full_version'])
                if canonicalize_name(entry['name']) in sources:
                    entry['source'] = sources[canonicalize_name(entry['name'])]
                entries.append(entry)
            staging.validate_dependencies(staged, python_version=info['full_version'])
            (Path(temporary) / 'manifest.json').write_text(json.dumps(dict(schema=1, packages=entries,
                python_full_version=info['full_version']), indent=2))
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(temporary, destination)
        options = _generation_options(build, config)
        platform_frameworks = list((cache / 'platform/frameworks').glob('*.framework'))
        replacements = {path.name for path in platform_frameworks}
        options['embed_frameworks'] = [path for path in options.get('embed_frameworks', []) if Path(path).name not in replacements]
        options['embed_frameworks'].extend(map(str, platform_frameworks))
        generate(application=root, app_dir=config['app_dir'], packages_dir=destination / 'packages',
                 python_framework=framework, python_lib=library, project_python=compiler, output=build,
                 bundle_id=config['bundle_id'], entry_module=config['entry_module'], **options)
    return json.loads((build / 'host-build.json').read_text()), compiler, info


def main():
    from meltygui.platforms.ios.application import read_application
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', type=Path, default=Path.cwd())
    parser.add_argument('--build', type=Path)
    parser.add_argument('--project-python', type=Path)
    args = parser.parse_args()
    application = read_application(args.application)
    root = application['root'].resolve()
    build = args.build or Path(application.get('ios', {}).get('build_directory', 'build/ios'))
    build = (root / build).resolve()
    try:
        config = json.loads((build / 'host-build.json').read_text())
        if args.project_python:
            config['project_python'] = runtime.project_python(root, args.project_python)
        with (build / '.run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _, _, info = ensure_configuration(root, build, config)
        print(f"iOS build inputs ready for project CPython {info['version']}")
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Could not prepare iOS build inputs: {error}\n')


if __name__ == '__main__':
    main()
