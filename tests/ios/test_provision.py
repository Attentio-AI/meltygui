"""Missing target inputs are prepared on the build host and reused on later runs."""
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import struct
import sys
import tarfile
import zipfile

import pytest
from packaging.requirements import Requirement

from meltygui.platforms.ios import provision, runtime, devices, generate


def test_pynacl_builds_and_reuses_static_device_sodium(tmp_path, monkeypatch):
    import shlex
    source = tmp_path / 'PyNaCl-1.6.2'
    cache = tmp_path / 'cache'
    commands = []
    monkeypatch.setattr(provision.subprocess, 'check_output', lambda *a, **kw: '/Xcode/clang\n')

    def run(command, *, env, log, directory):
        command = list(map(str, command))
        commands.append(command)
        assert env['CC'] == '/Xcode/clang'
        assert shlex.split(env['CFLAGS'])[:4] == ['-target', 'arm64-apple-ios17.0', '-isysroot', '/iPhone SDK']
        assert 'CPATH' not in env and env['PKG_CONFIG'] == '/usr/bin/false'
        if command[-1] == 'install':
            for path in ('lib/libsodium.a', 'include/sodium.h'):
                target = directory / 'install' / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('device artifact')

    monkeypatch.setattr(provision, '_run', run)
    host = dict(SDKROOT='/iPhone SDK', CPATH='/opt/homebrew/include', LDFLAGS='-L/host/lib')
    result = provision._pynacl_environment(source, cache, host)
    assert '--host=aarch64-apple-darwin' in commands[0]
    assert '--disable-shared' in commands[0]
    assert len(commands) == 3
    assert result['SODIUM_INSTALL'] == 'system'
    assert '/host/lib' not in result['LDFLAGS'] and 'homebrew' not in result['CFLAGS']
    assert provision._pynacl_environment(source, cache, host) == result
    assert len(commands) == 3
    (cache / 'pynacl-deps' / source.name / 'install/lib/libsodium.a').unlink()
    provision._pynacl_environment(source, cache, host)
    assert len(commands) == 6


def binary():
    command = struct.pack('<6I', 0x32, 24, 2, 17 << 16, 17 << 16, 0)
    return struct.pack('<8I', 0xFEEDFACF, 0x0100000C, 0, 6, 1, len(command), 0, 0) + command


def info(version):
    return dict(version=version, full_version=version + '.1', magic='test', implementation='cpython',
                cache_tag='cpython-' + version.replace('.', ''))


def support_archive(root, version):
    archive = root / f'Python-{version}-iOS-support.test.tar.gz'
    prefix = 'Python.xcframework'
    entries = {
        f'{prefix}/ios-arm64/Python.framework/Python':
            struct.pack('>7I', 0xCAFEBABE, 1, 0x0100000C, 0, 28, len(binary()), 0) + binary(),
        f'{prefix}/ios-arm64/Python.framework/Headers/patchlevel.h':
            f'#define PY_MAJOR_VERSION 3\n#define PY_MINOR_VERSION {version.split(".")[1]}\n'.encode(),
        f'{prefix}/lib/python{version}/encodings/__init__.py': b'# stdlib',
        f'{prefix}/ios-arm64/lib-arm64/python{version}/lib-dynload/_native.so': binary(),
        f'{prefix}/ios-arm64_x86_64-simulator/lib-arm64/python{version}/lib-dynload/simulator.so': b'not a device',
        f'{prefix}/ios-arm64/platform-config/arm64-iphoneos/make_cross_venv.py': b'# cross environment',
    }
    with tarfile.open(archive, 'w:gz') as target:
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    return archive


def wheel(root, version='3.12', name='native', release='1.0'):
    abi = version.replace('.', '')
    path = root / f'{name}-{release}-cp{abi}-cp{abi}-ios_17_0_arm64_iphoneos.whl'
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'{name}-{release}.dist-info/METADATA',
                         f'Metadata-Version: 2.3\nName: {name}\nVersion: {release}\nRequires-Python: >=3.10\n')
        archive.writestr(f'{name}-{release}.dist-info/WHEEL',
                         f'Wheel-Version: 1.0\nTag: cp{abi}-cp{abi}-ios_17_0_arm64_iphoneos\n')
        archive.writestr(f'{name}/_native.cpython-{abi}-iphoneos.so', binary())
    return path


@pytest.mark.parametrize('version', ['3.10', '3.11', '3.12'])
def test_runtime_download_normalizes_stdlib_and_caches_device_slice(tmp_path, monkeypatch, version):
    archive = support_archive(tmp_path, version)
    calls = []
    def releases(url):
        calls.append(url)
        return [{'assets': [{'name': archive.name, 'browser_download_url': 'https://example/runtime',
                             'digest': 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest()}]}]
    monkeypatch.setattr(provision, '_json', releases)
    monkeypatch.setattr(provision, '_download', lambda *args: archive)
    cache = tmp_path / 'cache'
    framework, library, cross = provision.ensure_runtime(info(version), sys.executable, cache, {})
    assert runtime.framework_version(framework) == version
    assert (library / f'python{version}/encodings/__init__.py').is_file()
    assert (library / f'python{version}/lib-dynload/_native.so').read_bytes() == binary()
    assert not list(library.rglob('simulator.so'))
    assert (cross / 'make_cross_venv.py').is_file()
    assert provision.ensure_runtime(info(version), sys.executable, cache, {}) == (framework, library, cross)
    assert len(calls) == 1


def test_missing_runtime_release_builds_from_source(tmp_path, monkeypatch):
    archive = support_archive(tmp_path, '3.12')
    monkeypatch.setattr(provision, '_json', lambda url: [])
    calls = []
    def run(command, **kwargs):
        calls.append(list(map(str, command)))
        if command[0] == '/usr/bin/make':
            destination = tmp_path / 'cache/runtime/source/dist'
            destination.mkdir(parents=True)
            (destination / archive.name).write_bytes(archive.read_bytes())
    monkeypatch.setattr(provision, '_run', run)
    framework, _, _ = provision.ensure_runtime(info('3.12'), sys.executable, tmp_path / 'cache', {})
    assert framework.is_dir()
    assert calls[0][:2] == ['git', 'clone']
    assert 'TARGETS-iOS=iphoneos.arm64' in calls[1]
    assert f'HOST_PYTHON={sys.executable}' in calls[1]


@pytest.mark.parametrize('version', ['3.10', '3.11', '3.12'])
def test_missing_wheel_builds_source_once_then_reuses_cache(tmp_path, monkeypatch, version):
    sdist = tmp_path / 'native.tar.gz'
    with tarfile.open(sdist, 'w:gz') as archive:
        member = tarfile.TarInfo('native-1.0/pyproject.toml')
        member.size = 0
        archive.addfile(member, io.BytesIO())
    calls = []
    monkeypatch.setattr(provision, '_json', lambda url: {'releases': {'1.0': [
        dict(filename=sdist.name, url='https://example/source', packagetype='sdist',
             requires_python='>=3.10', digests={'sha256': 'test'})]}})
    monkeypatch.setattr(provision, '_download', lambda *args: sdist)
    def build(name, source, compiler, selected, *args):
        calls.append(selected['version'])
        assert (source / 'pyproject.toml').is_file()
        return wheel(tmp_path / 'built', selected['version'])
    monkeypatch.setattr(provision, '_build_source', build)
    arguments = (Requirement('native'), sys.executable, info(version), tmp_path, tmp_path, tmp_path / 'cache', {})
    first = provision.acquire_wheel(*arguments)
    second = provision.acquire_wheel(*arguments)
    assert first == second
    assert first.parent.name == 'wheels'
    assert calls == [version]
    first.write_bytes(b'interrupted cache write')
    repaired = provision.acquire_wheel(*arguments)
    assert repaired == first
    assert zipfile.is_zipfile(repaired)
    assert calls == [version, version]


def test_compatible_published_wheel_does_not_compile(tmp_path, monkeypatch):
    published = wheel(tmp_path)
    monkeypatch.setattr(provision, '_json', lambda url: {'releases': {'1.0': [
        dict(filename=published.name, url='https://example/wheel', packagetype='bdist_wheel',
             requires_python='>=3.10', digests={'sha256': 'test'})]}})
    monkeypatch.setattr(provision, '_download', lambda url, target, digest: published)
    monkeypatch.setattr(provision, '_build_source', lambda *args: pytest.fail('should use the published wheel'))
    assert provision.acquire_wheel(Requirement('native'), sys.executable, info('3.12'),
                                   tmp_path, tmp_path, tmp_path / 'cache', {}) == published


def configuration(tmp_path, monkeypatch):
    (tmp_path / '.venv/bin').mkdir(parents=True)
    (tmp_path / '.venv/bin/python').symlink_to(sys.executable)
    (tmp_path / 'pyproject.toml').write_text('[tool.melty.app]\ndependencies = ["native"]\n')
    build = tmp_path / 'build'
    (build / 'app').mkdir(parents=True)
    (build / 'app/main.py').write_text('pass\n')
    config = dict(app_dir=str(build / 'app'), packages_dir=str(build / 'old-packages'),
                  python_lib=str(build / 'old-lib'), entry_module='main', bundle_id='test.app',
                  generator=dict(team='ORIGINALTEAM', toolkit_dir=str(provision.ROOT.parents[1]),
                                 renderer_sources=[], embed_frameworks=[]))
    (build / 'host-build.json').write_text(json.dumps(config))
    monkeypatch.setattr(runtime, 'python_info', lambda executable: info('3.12'))
    monkeypatch.setattr(generate, 'python_info', lambda executable: info('3.12'))
    monkeypatch.setattr(devices, 'xcode_environment', lambda: {'DEVELOPER_DIR': '/Xcode/Contents/Developer'})
    monkeypatch.setattr(provision.subprocess, 'run', lambda *a, **k: type('Result', (), {'stdout': '26.0\n'})())
    return build, config


def test_device_preparation_replaces_old_runtime_and_preserves_signing(tmp_path, monkeypatch):
    build, config = configuration(tmp_path, monkeypatch)
    archive = support_archive(tmp_path, '3.12')
    monkeypatch.setattr(provision, '_json', lambda url: [{'assets': [dict(name=archive.name, browser_download_url='https://example/runtime')]}])
    monkeypatch.setattr(provision, '_download', lambda *a: archive)
    calls = []
    def acquire(*args):
        calls.append(args[2]['version'])
        return wheel(tmp_path / 'built')
    monkeypatch.setattr(provision, 'acquire_wheel', acquire)
    updated, _, _ = provision.ensure_configuration(tmp_path, build, config, cache_root=tmp_path / 'cache')
    assert updated['python_version'] == '3.12'
    assert updated['generator']['team'] == 'ORIGINALTEAM'
    assert (Path(updated['packages_dir']) / 'native-1.0.dist-info/WHEEL').is_file()
    project = build / 'MeltyIOS.xcodeproj/project.pbxproj'
    before = project.stat().st_mtime_ns
    provision.ensure_configuration(tmp_path, build, updated, cache_root=tmp_path / 'cache')
    assert project.stat().st_mtime_ns == before
    # An older prepared app picks up new system frameworks without rebuilding wheels.
    old = dict(updated)
    old.pop('generation_inputs')
    objects = plistlib.loads(project.read_bytes())
    for obj in objects['objects'].values():
        if obj.get('path', '').endswith('/Network.framework'):
            obj['path'] = obj['path'].replace('Network.framework', 'Old.framework')
    project.write_bytes(plistlib.dumps(objects))
    refreshed, _, _ = provision.ensure_configuration(tmp_path, build, old, cache_root=tmp_path / 'cache')
    assert refreshed['generation_inputs'] == generate.generation_inputs()
    assert b'/Network.framework' in project.read_bytes()
    assert calls == ['3.12']


def test_failed_preparation_does_not_replace_project_config(tmp_path, monkeypatch):
    build, config = configuration(tmp_path, monkeypatch)
    original = (build / 'host-build.json').read_bytes()
    def failed(*args):
        raise RuntimeError('compiler failed: actual diagnostic')
    monkeypatch.setattr(provision, 'ensure_runtime', failed)
    with pytest.raises(RuntimeError, match='actual diagnostic'):
        provision.ensure_configuration(tmp_path, build, config, cache_root=tmp_path / 'cache')
    assert (build / 'host-build.json').read_bytes() == original


@pytest.mark.parametrize('version', ['3.10', '3.11', '3.12'])
@pytest.mark.parametrize('package', ['native', 'pillow', 'cffi'])
def test_cross_build_uses_owned_venv_and_target_configuration(tmp_path, monkeypatch, version, package):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'pyproject.toml').write_text('[build-system]\nrequires = ["setuptools"]\n')
    compiler = tmp_path / 'project/.venv/bin/python'
    framework = tmp_path / 'support/ios-arm64/Python.framework'
    platform_config = framework.parent / 'platform-config/arm64-iphoneos'
    cache = tmp_path / 'cache'
    owned = cache / f'builds/{package}-1.0/venv'
    pth = owned / f'lib/python{version}/site-packages/_cross_venv.pth'
    calls = []

    def run(command, *, env, log):
        command = list(map(str, command))
        calls.append((command, env))
        if command[1:3] == ['-m', 'venv']:
            assert command[0] == str(compiler)
            (owned / 'bin').mkdir(parents=True)
            (owned / 'bin/python').touch()
        elif command[1:4] == ['-m', 'pip', 'install']:
            assert command[0] == str(owned / 'bin/python')
            assert not pth.exists(), 'Host tools must install before enabling cross mode'
        elif command[1] == str(platform_config / 'make_cross_venv.py'):
            assert command[0] == str(compiler)
            assert command[2] == str(owned)
            pth.parent.mkdir(parents=True, exist_ok=True)
            pth.write_text('import _cross_venv\n')
        elif command[1] == '-c':
            assert pth.is_file()
            if package == 'cffi':
                assert env['CFLAGS'] == '-I/target/libffi/include'
            Path(command[-1]).write_text('["setuptools-rust"]')
        elif command[1:3] == ['-m', 'build']:
            assert pth.is_file()
            assert '--no-isolation' in command
            assert env['PATH'].split(os.pathsep)[0] == str(framework.parent / 'bin')
            assert env['CARGO_BUILD_TARGET'] == 'aarch64-apple-ios'
            assert env['SDKROOT'] == '/iPhoneSDK'
            assert env['CARGO_TARGET_AARCH64_APPLE_IOS_LINKER'] == '/Xcode/clang'
            assert env['CC_aarch64_apple_ios'] == '/Xcode/clang'
            assert '-isysroot /iPhoneSDK' in env['CFLAGS_aarch64_apple_ios']
            assert 'link-arg=/iPhoneSDK' in env['RUSTFLAGS']
            assert 'MACOSX_DEPLOYMENT_TARGET' not in env
            assert 'CARGO_ENCODED_RUSTFLAGS' not in env
            assert env['PYO3_CROSS_PYTHON_VERSION'] == version
            assert f'version={version}\n' in Path(env['PYO3_CONFIG_FILE']).read_text()
            assert f"ext_suffix=.cpython-{version.replace('.', '')}-iphoneos.so\n" in Path(env['PYO3_CONFIG_FILE']).read_text()
            if package == 'pillow':
                assert env['JPEG_ROOT'] == '/target/jpeg'
                assert env['ZLIB_ROOT'] == '/target/sdk/usr'
                settings = [command[index + 1] for index, value in enumerate(command) if value == '-C']
                assert 'jpeg=enable' in settings and 'zlib=enable' in settings
                assert 'platform-guessing=disable' in settings
            else:
                assert '-C' not in command
            if package == 'cffi':
                assert env['LDFLAGS'] == '-L/target/libffi/lib'
            wheel(Path(command[command.index('--outdir') + 1]), version)
        else:
            pytest.fail(f'Unexpected build command: {command}')

    monkeypatch.setattr(provision, '_run', run)
    monkeypatch.setattr(provision.subprocess, 'check_output', lambda command, **kwargs:
                        '/iPhoneSDK\n' if command[-1] == '--show-sdk-path' else '/Xcode/clang\n')
    monkeypatch.setattr(provision, '_rust', lambda cache, env: env)
    monkeypatch.setattr(provision, '_pillow_environment', lambda source, cache, env:
                        dict(env, JPEG_ROOT='/target/jpeg', ZLIB_ROOT='/target/sdk/usr'))
    monkeypatch.setattr(provision, '_cffi_environment', lambda source, cache, env:
                        dict(env, CFLAGS='-I/target/libffi/include', LDFLAGS='-L/target/libffi/lib'))
    result = provision._build_source(f'{package}-1.0', source, compiler, info(version), framework,
                                     platform_config, cache, dict(PATH='/usr/bin', SDKROOT='/MacSDK',
                                         MACOSX_DEPLOYMENT_TARGET='11.0', CARGO_ENCODED_RUSTFLAGS='host flags'))
    assert result.is_file()
    assert not compiler.parent.exists(), 'The project venv must not be modified'
    assert sum(command[1] == str(platform_config / 'make_cross_venv.py') for command, env in calls) == 2


def test_pillow_builds_and_caches_device_jpeg_with_sdk_zlib(tmp_path, monkeypatch):
    archive = tmp_path / 'jpeg.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        for filename in ('src/jpeglib.h', 'src/jmorecfg.h', 'src/jerror.h', 'LICENSE.md', 'README.ijg'):
            data = f'jpeg {filename}'.encode()
            member = tarfile.TarInfo(f'libjpeg-turbo-{provision.JPEG_VERSION}/{filename}')
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
    downloads, commands = [], []
    def download(url, path, checksum):
        downloads.append((url, checksum))
        return archive
    monkeypatch.setattr(provision, '_download', download)
    monkeypatch.setattr(provision, '_tools', lambda *args: Path('/tools/bin/python'))
    def capture(command, **kwargs):
        assert command[:3] == ['/usr/bin/xcrun', '--sdk', 'iphoneos']
        assert kwargs['close_fds'] is False
        return '/iPhone SDK\n' if command[-1] == '--show-sdk-path' else '/Xcode/clang\n'
    monkeypatch.setattr(provision.subprocess, 'check_output', capture)
    def run(command, *, env, log):
        command = list(map(str, command))
        commands.append(command)
        assert env['SDKROOT'] == '/iPhone SDK'
        assert 'CPATH' not in env and 'CFLAGS' not in env
        assert 'WEBP_ROOT' not in env
        if '-S' in command:
            assert '--fresh' in command
            assert '-DCMAKE_SYSTEM_NAME=iOS' in command
            assert '-DCMAKE_SYSTEM_PROCESSOR=arm64' in command
            assert '-DCMAKE_OSX_ARCHITECTURES=arm64' in command
            assert '-DCMAKE_OSX_SYSROOT=/iPhone SDK' in command
            assert '-DENABLE_SHARED=OFF' in command
            assert '-DENABLE_STATIC=ON' in command
            assert '-DCMAKE_OSX_DEPLOYMENT_TARGET=17.0' in command
        else:
            assert command[command.index('--target') + 1] == 'jpeg-static'
            native = Path(command[command.index('--build') + 1])
            native.mkdir(parents=True, exist_ok=True)
            (native / 'libjpeg.a').write_bytes(b'!<arch>\n')
            (native / 'jconfig.h').write_text('#define JPEG_LIB_VERSION 62\n')
    monkeypatch.setattr(provision, '_run', run)
    source = tmp_path / 'pillow'
    source.mkdir()
    (source / 'LICENSE').write_text('Pillow license\n')
    host_env = dict(PATH='/usr/bin', CPATH='/opt/homebrew/include', CFLAGS='-I/opt/homebrew/include',
                    WEBP_ROOT='/opt/homebrew', PKG_CONFIG='/opt/homebrew/bin/pkg-config')
    arguments = source, tmp_path / 'cache with spaces', host_env
    env = provision._pillow_environment(*arguments)
    prefix = Path(env['JPEG_ROOT'])
    assert (prefix / 'lib/libjpeg.a').is_file()
    assert (prefix / 'include/jerror.h').is_file()
    assert env['ZLIB_ROOT'] == '/iPhone SDK/usr'
    assert env['PKG_CONFIG'] == '/usr/bin/false'
    assert host_env['CPATH'] == '/opt/homebrew/include'
    assert 'jpeg README.ijg' in (source / 'LICENSE').read_text()
    assert downloads[0][1] == provision.JPEG_SHA256
    assert provision._pillow_environment(*arguments) == env
    assert len(commands) == 2 and len(downloads) == 1
    assert (source / 'LICENSE').read_text().count('Bundled libjpeg-turbo') == 1
    (prefix / 'include/jerror.h').unlink()
    provision._pillow_environment(*arguments)
    assert len(commands) == 4
    assert (prefix / 'include/jerror.h').is_file()


def test_build_failure_keeps_actionable_log(tmp_path):
    log = tmp_path / 'compile.log'
    with pytest.raises(RuntimeError, match='undefined symbol: PyExample') as error:
        provision._run([sys.executable, '-c', 'print("undefined symbol: PyExample"); raise SystemExit(2)'],
                       env=dict(os.environ), log=log, directory=tmp_path)
    assert str(log) in str(error.value)
    assert 'undefined symbol: PyExample' in log.read_text()


def test_build_failure_reports_early_cmake_error_from_this_attempt(tmp_path):
    log = tmp_path / 'compile.log'
    log.write_text('CMake Error: obsolete failure from a previous attempt\n')
    program = ('print("CMake Error at CMakeLists.txt:108 (string):\\n  string no output variable specified"); '
               'print("configuration progress\\n" * 80); raise SystemExit(1)')
    with pytest.raises(RuntimeError) as error:
        provision._run([sys.executable, '-c', program], env=dict(os.environ), log=log)
    message = str(error.value)
    assert 'string no output variable specified' in message
    assert 'obsolete failure' not in message


def test_cffi_uses_cached_iphone_headers_and_static_library(tmp_path, monkeypatch):
    import shlex
    archive = tmp_path / 'libffi.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        for filename in ('include/ffi.h', 'include/ffi_arm64.h', 'include/ffitarget_arm64.h', 'lib/libffi.a'):
            data = b'/* Copyright libffi authors. Test license. */\n'
            member = tarfile.TarInfo(filename)
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
    downloads = []
    def download(url, path, checksum):
        downloads.append(url)
        assert 'iphoneos.arm64' in url
        assert checksum == provision.LIBFFI_SHA256
        return archive
    monkeypatch.setattr(provision, '_download', download)
    source = tmp_path / 'cffi'
    source.mkdir()
    (source / 'LICENSE').write_text('CFFI license\n')
    cache = tmp_path / 'cache with spaces'
    host = dict(CFLAGS='-I/opt/homebrew/include', LDFLAGS='-L/opt/homebrew/lib', PATH='/usr/bin')
    result = provision._cffi_environment(source, cache, host)
    prefix = cache / 'cffi-deps' / f'libffi-{provision.LIBFFI_VERSION}'
    assert shlex.split(result['CFLAGS']) == ['-I' + str(prefix / 'include')]
    assert shlex.split(result['LDFLAGS']) == ['-L' + str(prefix / 'lib')]
    assert result['PKG_CONFIG'] == '/usr/bin/false'
    assert host['CFLAGS'] == '-I/opt/homebrew/include'
    assert (prefix / 'lib/libffi.a').is_file()
    assert provision._cffi_environment(source, cache, host) == result
    assert len(downloads) == 1
    assert (source / 'LICENSE').read_text().count('Copyright libffi authors') == 1


def test_old_python_acquires_compatible_alternative_to_catalogue_pin(tmp_path, monkeypatch):
    staging = provision.staging
    old = wheel(tmp_path / 'old', '3.10')
    incompatible = wheel(tmp_path / 'new', '3.13', release='2.0')
    monkeypatch.setattr(staging, 'download', lambda *a: tmp_path)
    monkeypatch.setattr(staging, 'pure_wheel', lambda *a: incompatible)
    original_metadata = staging.wheel_metadata
    def metadata(path):
        value = original_metadata(path)
        if path == incompatible:
            value.replace_header('Requires-Python', '>=3.13')
        return value
    monkeypatch.setattr(staging, 'wheel_metadata', metadata)
    requests = []
    def acquire(requirement):
        requests.append(str(requirement))
        return old
    selected = staging.resolve_wheels(['native'], [], [{'name': 'native'}], tmp_path,
                                     python_version='3.10.1', acquire_wheel=acquire)
    assert selected == [old]
    assert requests == ['native']
