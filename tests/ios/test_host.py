"""Host contracts that can be checked without Xcode or an iOS SDK."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import shutil
import sys
import tempfile
import types
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[2] / "meltygui/platforms/ios"
PYTHON_VERSION = "%d.%d" % sys.version_info[:2]
PYTHON_ABI = PYTHON_VERSION.replace(".", "")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from meltygui.platforms.ios import prepare_bundle as bundle
from meltygui.platforms.ios import generate as generator


def binary(platform=2, cpu=0x0100000C):
    command = struct.pack("<6I", 0x32, 24, platform, 17 << 16, 17 << 16, 0)
    return struct.pack("<8I", 0xFEEDFACF, cpu, 0, 6, 1, len(command), 0, 0) + command


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runtime = self.root / "Python.framework"
        (self.runtime / "Headers").mkdir(parents=True)
        (self.runtime / "Headers/patchlevel.h").write_text(f"#define PY_MAJOR_VERSION {sys.version_info.major}\n#define PY_MINOR_VERSION {sys.version_info.minor}\n")
        (self.runtime / "Python").write_bytes(binary())
        self.library = self.root / "lib"
        (self.library / f"python{PYTHON_VERSION}/encodings").mkdir(parents=True)
        (self.library / f"python{PYTHON_VERSION}/encodings/__init__.py").touch()
        (self.library / f"python{PYTHON_VERSION}/lib-dynload").mkdir()
        self.app = self.root / "source app"
        self.app.mkdir()
        (self.app / ".venv/bin").mkdir(parents=True)
        (self.app / ".venv/bin/python").symlink_to(sys.executable)
        (self.app / "main.py").write_text("print('on device')\n")
        self.packages = self.root / "packages"
        self.packages.mkdir()
        self.target = self.root / "Melty.app"
        self.target.mkdir()
        self.config = dict(python_lib=str(self.library), python_framework=str(self.runtime), app_dir=str(self.app),
                           packages_dir=str(self.packages), bootstrap_dir=str(ROOT / "Python"),
                           bundle_id="local.melty.counter", entry_module="main")

    def test_precompiled_libraries_load_after_install_without_reading_source(self):
        (self.packages / 'cached.py').write_text('def value():\n    return __debug__\n')
        # Build-machine cache settings must not send bytecode outside the app.
        with mock.patch.object(sys, 'pycache_prefix', str(self.root / 'outside-cache')):
            bundle.prepare(self.config, self.target, signing_allowed=False)
        self.assertFalse((self.root / 'outside-cache').exists())
        self.assertTrue(list((self.target / 'host').rglob('*.pyc')))
        self.assertTrue(list((self.target / f'python/lib/python{PYTHON_VERSION}').rglob('*.pyc')))
        self.assertFalse(list((self.target / 'app').rglob('*.pyc')))

        installed = self.root / 'installed.app'
        shutil.move(self.target, installed)
        source = installed / 'app_packages/cached.py'
        os.utime(source, (1000, 1000))  # Installation can change source metadata.
        spec = importlib.util.spec_from_file_location('cached', source)
        module = importlib.util.module_from_spec(spec)
        get_data = spec.loader.get_data

        def read_bytecode_only(path):
            self.assertTrue(path.endswith('.pyc'), f'Unexpected source read: {path}')
            return get_data(path)

        with mock.patch.object(spec.loader, 'get_data', side_effect=read_bytecode_only), \
             mock.patch.object(sys, 'dont_write_bytecode', True):
            spec.loader.exec_module(module)
        self.assertIs(module.value(), True)  # -O on the build host must not strip assertions.
        self.assertEqual(module.__file__, str(source))
        self.assertEqual(module.value.__code__.co_filename, str(source))
        self.assertIn('def value', source.read_text())

    def test_rebuild_replaces_bytecode_and_leaves_app_edits_uncached(self):
        source = self.packages / 'cached.py'
        source.write_text('value = 1\n')
        bundle.prepare(self.config, self.target, signing_allowed=False)
        source.write_text('value = 2\n')
        bundle.prepare(self.config, self.target, signing_allowed=False)
        with mock.patch.object(sys, 'dont_write_bytecode', True):
            library = load('cached', self.target / 'app_packages/cached.py')
            self.assertEqual(library.value, 2)
            app_source = self.target / 'app/main.py'
            for value in (1, 2):
                app_source.write_text(f'value = {value}\n')
                os.utime(app_source, (1000, 1000))
                self.assertEqual(load('main', app_source).value, value)
        self.assertFalse(list((self.target / 'app').rglob('*.pyc')))

    def test_invalid_test_fixtures_remain_source_only(self):
        (self.packages / 'invalid.py').write_text('def broken(\n')
        (self.packages / 'valid.py').write_text('value = 1\n')
        with contextlib.redirect_stdout(io.StringIO()):
            bundle.prepare(self.config, self.target, signing_allowed=False)
        self.assertEqual((self.target / 'app_packages/invalid.py').read_text(), 'def broken(\n')
        self.assertFalse(list((self.target / 'app_packages').rglob('invalid.*.pyc')))
        self.assertTrue(list((self.target / 'app_packages').rglob('valid.*.pyc')))

    def test_mismatched_runtime_fails_before_modifying_bundle(self):
        sentinel = self.target / 'host/keep.txt'
        sentinel.parent.mkdir()
        sentinel.write_text('previous build')
        (self.runtime / 'Headers/patchlevel.h').write_text('#define PY_MAJOR_VERSION 3\n#define PY_MINOR_VERSION 99\n')
        with self.assertRaisesRegex(ValueError, 'matching the project venv'):
            bundle.prepare(self.config, self.target, signing_allowed=False)
        self.assertEqual(sentinel.read_text(), 'previous build')

    def test_optimized_build_python_produces_unoptimized_device_bytecode(self):
        (self.packages / 'cached.py').write_text('value = __debug__\n')
        program = ('import json, sys; from meltygui.platforms.ios.prepare_bundle import prepare; '
                   'prepare(json.loads(sys.argv[1]), sys.argv[2], signing_allowed=False)')
        result = subprocess.run([sys.executable, '-I', '-O', '-c', program,
                                 json.dumps(self.config), str(self.target)],
                                close_fds=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.target / 'app_packages/cached.py'
        spec = importlib.util.spec_from_file_location('cached', source)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(spec.loader, 'source_to_code', side_effect=AssertionError('cache miss')), \
             mock.patch.object(sys, 'dont_write_bytecode', True):
            spec.loader.exec_module(module)
        self.assertIs(module.value, True)

    def test_compiles_with_project_venv_when_build_python_differs(self):
        from meltygui.platforms.ios.runtime import python_info
        other_version = '3.13' if PYTHON_VERSION != '3.13' else '3.12'
        other_python = shutil.which(f'python{other_version}')
        if not other_python:
            self.skipTest(f'CPython {other_version} is not installed')
        venv = self.root / 'other project venv'
        subprocess.run([other_python, '-m', 'venv', '--without-pip', str(venv)],
                       check=True, close_fds=False, capture_output=True)
        compiler = venv / 'bin/python'
        info = python_info(compiler)
        self.assertNotEqual(info['version'], PYTHON_VERSION)
        (self.runtime / 'Headers/patchlevel.h').write_text(
            f"#define PY_MAJOR_VERSION 3\n#define PY_MINOR_VERSION {other_version.split('.')[1]}\n")
        (self.library / f'python{PYTHON_VERSION}').rename(self.library / f'python{other_version}')
        self.config['project_python'] = str(compiler)
        (self.packages / 'cached.py').write_text('value = 42\n')
        bundle.prepare(self.config, self.target, signing_allowed=False)
        caches = list((self.target / 'app_packages').rglob('*.pyc'))
        self.assertEqual(len(caches), 1)
        self.assertEqual(caches[0].read_bytes()[:4].hex(), info['magic'])
        self.assertIn(info['cache_tag'], caches[0].name)
        program = '''import importlib.util, sys
spec = importlib.util.spec_from_file_location('cached', sys.argv[1])
def no_compile(*args, **kwargs):
    raise AssertionError('device should use precompiled bytecode')
spec.loader.source_to_code = no_compile
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.value == 42
'''
        subprocess.run([str(compiler), '-I', '-B', '-c', program,
                        str(self.target / 'app_packages/cached.py')], check=True, close_fds=False)

    def test_rejects_native_modules_from_another_python(self):
        (self.packages / '_wrong.cpython-399-iphoneos.so').write_bytes(binary())
        with self.assertRaisesRegex(ValueError, 'does not match project CPython'):
            bundle.prepare(self.config, self.target, signing_allowed=False)

    def test_rejects_arm64_simulator_and_macos(self):
        path = self.root / "extension.so"
        for platform in (1, 7, 6):
            with self.subTest(platform=platform):
                path.write_bytes(binary(platform))
                with self.assertRaisesRegex(ValueError, "Expected iOS device"):
                    bundle.validate_device_binary(path)
        path.write_bytes(binary())
        bundle.validate_device_binary(path)
        path.write_bytes(binary(cpu=0x01000007))
        with self.assertRaisesRegex(ValueError, "thin ARM64"):
            bundle.validate_device_binary(path)

    def test_truncated_binary_is_rejected(self):
        path = self.root / "bad.so"
        for data in (b"bad", binary()[:-4]):
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                bundle.validate_device_binary(path)

    def test_each_build_gets_a_new_application_source_generation(self):
        bundle.prepare(self.config, self.target, signing_allowed=False)
        first = plistlib.loads((self.target / 'HostSettings.plist').read_bytes())
        bundle.prepare(self.config, self.target, signing_allowed=False)
        second = plistlib.loads((self.target / 'HostSettings.plist').read_bytes())
        self.assertEqual(first['entry_module'], 'main')
        self.assertEqual(second['entry_module'], 'main')
        self.assertNotEqual(uuid.UUID(first['source_generation']), uuid.UUID(second['source_generation']))

    def test_packages_signed_framework_and_bidirectional_markers(self):
        package = self.packages / "nested"
        package.mkdir()
        (package / f"_native.cpython-{PYTHON_ABI}-iphoneos.so").write_bytes(binary())
        real_run = subprocess.run
        sign = mock.Mock()
        def run(command, **kwargs):
            if command[0] == '/usr/bin/codesign':
                return sign(command, **kwargs)
            return real_run(command, **kwargs)
        with mock.patch.object(bundle.subprocess, "run", side_effect=run):
            modules = bundle.prepare(self.config, self.target, identity="test-identity")
        self.assertEqual(modules, {"nested._native"})
        marker = self.target / f"app_packages/nested/_native.cpython-{PYTHON_ABI}-iphoneos.fwork"
        executable = self.target / marker.read_text().strip()
        self.assertEqual(executable.read_bytes(), binary())
        self.assertEqual(Path(str(executable) + ".origin").read_text().strip(), marker.relative_to(self.target).as_posix())
        info = plistlib.loads((executable.parent / "Info.plist").read_bytes())
        self.assertEqual(info["CFBundleExecutable"], "nested._native")
        sign.assert_called_once_with(["/usr/bin/codesign", "--force", "--sign", "test-identity",
                                      "--timestamp=none", str(executable.parent)], check=True,
                                     close_fds=False, stderr=subprocess.PIPE, text=True)
        self.assertFalse(list((self.target / "app_packages").rglob("*.so")))
        # Rebuilding removes stale extension frameworks without touching supplied ones.
        (package / f"_native.cpython-{PYTHON_ABI}-iphoneos.so").unlink()
        supplied = self.target / "Frameworks/Python.framework"
        supplied.mkdir()
        bundle.prepare(self.config, self.target, signing_allowed=False)
        self.assertFalse(executable.parent.exists())
        self.assertTrue(supplied.is_dir())

    def test_signing_error_explains_locked_keychain_without_hiding_other_failures(self):
        (self.packages / f"_native.cpython-{PYTHON_ABI}-iphoneos.so").write_bytes(binary())
        real_run = subprocess.run
        for detail, expected in [('errSecInternalComponent', 'Unlock the login keychain'),
                                 ('invalid signing identity', 'invalid signing identity')]:
            def run(command, **kwargs):
                if command[0] == '/usr/bin/codesign':
                    raise subprocess.CalledProcessError(1, command, stderr=detail)
                return real_run(command, **kwargs)
            with self.subTest(detail=detail), mock.patch.object(bundle.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(ValueError, expected):
                    bundle.prepare(self.config, self.target, identity='test-identity')

    def test_never_packages_desktop_wheels(self):
        (self.packages / "desktop.so").write_bytes(binary(platform=1))
        with self.assertRaisesRegex(ValueError, "Expected iOS device"):
            bundle.prepare(self.config, self.target, signing_allowed=False)

    def test_excludes_checkout_artifacts_and_rejects_overlapping_paths(self):
        for directory in (".git", ".venv", "venv", "build", "dist", "__pycache__"):
            (self.app / directory).mkdir(exist_ok=True)
            (self.app / directory / "should-not-ship").touch()
        bundle.prepare(self.config, self.target, signing_allowed=False)
        self.assertEqual({path.name for path in (self.target / "app").iterdir()}, {"main.py"})
        with self.assertRaisesRegex(ValueError, "overlap"):
            bundle.copy_tree(self.app, self.app / "build/nested.app/app")
        with self.assertRaisesRegex(ValueError, "overlap"):
            bundle.copy_tree(self.app, self.app.parent)
        self.assertTrue(self.app.exists())

    def test_fails_on_unsigned_device_or_raw_dylib(self):
        with self.assertRaisesRegex(ValueError, "EXPANDED_CODE_SIGN_IDENTITY"):
            bundle.prepare(self.config, self.target)
        (self.packages / "dependency.dylib").write_bytes(binary())
        with self.assertRaisesRegex(ValueError, "dependent dylibs"):
            bundle.prepare(self.config, self.target, signing_allowed=False)

    def test_preserves_stdlib_and_package_names_that_match_checkout_artifacts(self):
        (self.library / f"python{PYTHON_VERSION}/venv").mkdir()
        (self.library / f"python{PYTHON_VERSION}/venv/__init__.py").write_text("# stdlib module\n")
        for name in ("build", "venv", "dist"):
            (self.packages / name).mkdir()
            (self.packages / name / "__init__.py").write_text(f"# package {name}\n")
        (self.app / "resources/build").mkdir(parents=True)
        (self.app / "resources/build/template.py").write_text("# application resource\n")
        bundle.prepare(self.config, self.target, signing_allowed=False)
        self.assertEqual((self.target / f"python/lib/python{PYTHON_VERSION}/venv/__init__.py").read_text(), "# stdlib module\n")
        for name in ("build", "venv", "dist"):
            self.assertEqual((self.target / "app_packages" / name / "__init__.py").read_text(), f"# package {name}\n")
        self.assertEqual((self.target / "app/resources/build/template.py").read_text(), "# application resource\n")

    def test_generator_is_repeatable_device_only_and_shares_documents(self):
        arguments = dict(python_framework=self.runtime, python_lib=self.library, app_dir=self.app,
                         output=self.root / "output", team="EXAMPLETEAM")
        project = generator.generate(**arguments)
        config = json.loads((self.root / 'output/host-build.json').read_text())
        self.assertEqual(config['project_python'], str(self.app / '.venv/bin/python'))
        self.assertEqual(config['python_version'], PYTHON_VERSION)
        self.assertEqual(config['python_magic'], importlib.util.MAGIC_NUMBER.hex())
        before = (project / "project.pbxproj").read_bytes()
        generator.generate(**arguments)
        self.assertEqual(before, (project / "project.pbxproj").read_bytes())
        objects = plistlib.loads(before)["objects"]
        self.assertTrue(any(obj.get('path', '').endswith('/Network.framework')
                            for obj in objects.values()))
        for framework in ('Security', 'UniformTypeIdentifiers'):
            self.assertTrue(any(obj.get('path', '').endswith(f'/{framework}.framework')
                                for obj in objects.values()))
        import shlex
        phase, = [obj for obj in objects.values() if obj['isa'] == 'PBXShellScriptBuildPhase']
        self.assertIn(shlex.quote(sys.executable) + ' -m meltygui.platforms.ios.compile_shaders', phase['shellScript'])
        targets = [obj for obj in objects.values() if obj["isa"] == "XCBuildConfiguration" and obj["buildSettings"]]
        self.assertEqual(len(targets), 2)
        for obj in targets:
            settings = obj["buildSettings"]
            self.assertEqual(settings["ARCHS"], "arm64")
            self.assertEqual(settings["SUPPORTED_PLATFORMS"], "iphoneos")
            self.assertEqual(settings["DEVELOPMENT_TEAM"], "EXAMPLETEAM")
        info = plistlib.loads((ROOT / "Host/Info.plist").read_bytes())
        self.assertIs(info["UIFileSharingEnabled"], True)
        self.assertIs(info["LSSupportsOpeningDocumentsInPlace"], True)
        self.assertTrue(info["NSLocalNetworkUsageDescription"].strip())
        self.assertIn('_melty-access._tcp', info['NSBonjourServices'])
        self.assertIs(info["CADisableMinimumFrameDurationOnPhone"], True)
        self.assertIs(info["UIApplicationSceneManifest"]["UIApplicationSupportsMultipleScenes"], False)

    def test_generator_rejects_wrong_cpython_minor(self):
        (self.runtime / "Headers/patchlevel.h").write_text("#define PY_MAJOR_VERSION 3\n#define PY_MINOR_VERSION 99\n")
        with self.assertRaisesRegex(ValueError, "matching the project venv"):
            generator.generate(python_framework=self.runtime, python_lib=self.library, app_dir=self.app)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.original_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.original_cwd)
        self.bootstrap = load("isolated_bootstrap", ROOT / "Python/melty_ios_bootstrap.py")
        self.bundle = self.root / 'Melty.app'
        (self.bundle / 'app').mkdir(parents=True)
        (self.bundle / 'app/test_ios_application.py').write_text('value = 1\n')
        (self.bundle / 'HostSettings.plist').write_bytes(plistlib.dumps({
            'entry_module': 'test_ios_application', 'source_generation': uuid.uuid4().hex}))
        self.bootstrap.__file__ = str(self.bundle / 'host/melty_ios_bootstrap.py')
        self.output = []
        self.native = types.SimpleNamespace(write_log=self.output.append, request_frame=mock.Mock(),
                                            set_keyboard_visible=mock.Mock(), set_safe_zone=mock.Mock(),
                                            get_clipboard_text=mock.Mock(return_value="cached paste \N{SNOWMAN}"),
                                            set_clipboard_text=mock.Mock())
        self.config = {name: str(self.root / name) for name in ("documents", "workspace", "application_support", "cache")}
        self.config.update(app_id="melty-ios-test", renderer_available=True, entry_module="test_ios_application")
        self.app = types.SimpleNamespace(config=self.config, frame=mock.Mock(return_value=False), suspend=mock.Mock(),
                                        resume=mock.Mock(), close=mock.Mock())
        self.launch = mock.Mock(return_value=self.app)

    @contextlib.contextmanager
    def environment(self):
        with mock.patch.dict(sys.modules, {"_melty_ios": self.native}), \
             mock.patch.object(sys, 'path', [*sys.path, str(self.bundle / 'app')]), \
             mock.patch('meltygui.code.fileref._EDITABLE_ROOTS', []), \
             mock.patch('meltygui.code.fileref._EDITABLE_SOURCE_CACHE', {}), \
             mock.patch("meltygui.core.runtime.native_app.start_native_application", self.launch), \
             mock.patch.dict(os.environ), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield

    def test_native_library_and_certificate_paths_follow_the_installed_container(self):
        installed = self.root / "new-container/Melty.app"
        installed.parent.mkdir()
        shutil.move(self.bundle, installed)
        self.bundle = installed
        certificate = installed / "app_packages/certifi/cacert.pem"
        certificate.parent.mkdir(parents=True)
        certificate.write_text("bundled certificates")
        with self.environment(), mock.patch.object(self.bootstrap, "__file__", str(installed / "host/bootstrap.py")):
            os.environ.pop("SSL_CERT_FILE", None)
            self.bootstrap.initialize(self.config)
            self.assertEqual(os.environ["SPATIALINDEX_C_LIBRARY"],
                             str(installed / "Frameworks/spatialindex_c.framework/spatialindex_c"))
            self.assertEqual(os.environ["DYLD_FRAMEWORK_PATH"], str(installed / "Frameworks"))
            self.assertEqual(os.environ["SSL_CERT_FILE"], str(certificate))

    def test_app_updates_in_place_and_preserves_device_files_across_builds(self):
        from meltygui.code.fileref import is_editable_source
        original = self.bundle / 'app/test_ios_application.py'
        with self.environment():
            self.bootstrap.initialize(self.config)
            spec = importlib.util.find_spec('test_ios_application')
            source = Path(spec.origin)
            self.assertTrue(source.is_relative_to(Path(self.config['application_support'])))
            self.assertTrue(is_editable_source(source))
            self.assertEqual(source.read_text(), 'value = 1\n')
            source.write_text('value = 2\n')
            (source.parent / 'local.py').write_text('local = True\n')
            self.assertEqual(original.read_text(), 'value = 1\n')
            self.bootstrap.close()

        # A new install container with the SAME build must keep device edits.
        moved = self.root / 'moved-container/Melty.app'
        moved.parent.mkdir()
        shutil.move(self.bundle, moved)
        self.bundle = moved
        self.bootstrap.__file__ = str(moved / 'host/melty_ios_bootstrap.py')
        with self.environment():
            self.bootstrap.initialize(self.config)
            self.assertEqual(Path(importlib.util.find_spec('test_ios_application').origin), source)
            self.assertEqual(source.read_text(), 'value = 2\n')
            self.bootstrap.close()

        # Rebuilding updates managed code, retaining local files and the source path.
        (moved / 'app/test_ios_application.py').write_text('value = 3\n')
        (moved / 'HostSettings.plist').write_bytes(plistlib.dumps({
            'entry_module': 'test_ios_application', 'source_generation': uuid.uuid4().hex}))
        with self.environment():
            self.bootstrap.initialize(self.config)
            rebuilt = Path(importlib.util.find_spec('test_ios_application').origin)
            self.assertEqual(rebuilt.read_text(), 'value = 3\n')
            self.assertTrue((rebuilt.parent / 'local.py').exists())
            self.assertEqual(rebuilt, source)
            backups = list(source.parent.parent.glob('backups/*/test_ios_application.py'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'value = 2\n')

    def test_failed_copy_keeps_the_previous_device_source(self):
        with self.environment():
            self.bootstrap.initialize(self.config)
            source = Path(importlib.util.find_spec('test_ios_application').origin)
            source.write_text('value = 2\n')
            self.bootstrap.close()
        (self.bundle / 'HostSettings.plist').write_bytes(plistlib.dumps({
            'entry_module': 'test_ios_application', 'source_generation': uuid.uuid4().hex}))
        with self.environment(), mock.patch('shutil.copytree', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.bootstrap.initialize(self.config)
        self.assertEqual(source.read_text(), 'value = 2\n')

    def test_source_update_keeps_saves_and_removes_only_unchanged_managed_files(self):
        import json
        (self.bundle / 'app/obsolete.py').write_text('old = True')
        (self.bundle / 'app/edited.py').write_text('original = True')
        with self.environment():
            self.bootstrap.initialize(self.config)
            source = Path(importlib.util.find_spec('test_ios_application').origin)
            (source.parent / 'save.json').write_text('{"progress": 42}')
            (source.parent / 'edited.py').write_text('device_edit = True')
            self.bootstrap.close()
        update = Path(self.config['application_support']) / 'meltygui/app-update'
        token = uuid.uuid4().hex
        payload = update / token / 'app'
        payload.mkdir(parents=True)
        (payload / 'test_ios_application.py').write_text('updated = True')
        settings = plistlib.loads((self.bundle / 'HostSettings.plist').read_bytes())
        (update / 'update.json').write_text(json.dumps({
            'base_generation': settings['source_generation'], 'generation': token,
            'files': self.bootstrap._source_files(payload)}))
        with self.environment():
            self.bootstrap.initialize(self.config)
            self.assertEqual(Path(importlib.util.find_spec('test_ios_application').origin), source)
            self.assertEqual(source.read_text(), 'updated = True')
            self.assertEqual((source.parent / 'save.json').read_text(), '{"progress": 42}')
            self.assertFalse((source.parent / 'obsolete.py').exists())
            self.assertEqual((source.parent / 'edited.py').read_text(), 'device_edit = True')
            self.bootstrap.close()
        # Uploading files without committing a new descriptor cannot apply half an update.
        uncommitted = update / uuid.uuid4().hex / 'app'
        uncommitted.mkdir(parents=True)
        (uncommitted / 'test_ios_application.py').write_text('incomplete = True')
        with self.environment():
            self.bootstrap.initialize(self.config)
            self.assertEqual(source.read_text(), 'updated = True')

    def test_rebuild_with_unchanged_source_keeps_device_edits(self):
        with self.environment():
            self.bootstrap.initialize(self.config)
            source = Path(importlib.util.find_spec('test_ios_application').origin)
            source.write_text('device = True')
            self.bootstrap.close()
        (self.bundle / 'HostSettings.plist').write_bytes(plistlib.dumps({
            'entry_module': 'test_ios_application', 'source_generation': uuid.uuid4().hex}))
        with self.environment():
            self.bootstrap.initialize(self.config)
            self.assertEqual(source.read_text(), 'device = True')

    def test_legacy_source_tree_is_adopted_without_losing_local_files(self):
        import json
        with self.environment():
            self.bootstrap.initialize(self.config)
            source = Path(importlib.util.find_spec('test_ios_application').origin)
            (source.parent / 'save.json').write_text('local save')
            self.bootstrap.close()
        (source.parent.parent / 'state.json').unlink()  # previous bootstrap had no manifest
        (self.bundle / 'HostSettings.plist').write_bytes(plistlib.dumps({
            'entry_module': 'test_ios_application', 'source_generation': uuid.uuid4().hex}))
        with self.environment():
            self.bootstrap.initialize(self.config)
            self.assertEqual(Path(importlib.util.find_spec('test_ios_application').origin), source)
            self.assertEqual((source.parent / 'save.json').read_text(), 'local save')

    def test_native_search_path_can_use_a_symlinked_container(self):
        alias = self.root / 'container-alias'
        alias.symlink_to(self.bundle, target_is_directory=True)
        with self.environment():
            sys.path[-1] = str(alias / 'app')
            self.bootstrap.initialize(self.config)
            source = Path(importlib.util.find_spec('test_ios_application').origin)
            self.assertTrue(source.is_relative_to(Path(self.config['application_support'])))
            self.assertEqual(source.read_text(), 'value = 1\n')

    def test_renderer_absence_is_reported_before_importing_desktop_editor(self):
        self.config["renderer_available"] = False
        with self.environment(), self.assertRaisesRegex(RuntimeError, "requires MeltyMetalRenderer"):
            self.bootstrap.initialize(self.config)
        self.launch.assert_not_called()
        self.assertIsNone(self.bootstrap._app)
        self.assertTrue(Path(self.config["workspace"]).is_dir())

    def test_lifecycle_and_idle_frame_protocol(self):
        with self.environment():
            self.bootstrap.initialize(self.config)
            self.assertEqual(Path.cwd(), Path(self.config["workspace"]))
            self.bootstrap.resume()
            events = [{"kind": "touch_begin", "touch_id": 1, "timestamp": 5.25}]
            self.assertFalse(self.bootstrap.frame({"presentation_time": 5.3}, events))
            self.app.frame.assert_called_once_with({"presentation_time": 5.3}, events)
            self.bootstrap.presented()  # Older app adapters may omit this hook.
            self.bootstrap._host.request_frame()
            self.native.request_frame.assert_called_once()
            self.bootstrap._host.set_keyboard_visible(True)
            self.native.set_keyboard_visible.assert_called_once_with(True)
            self.bootstrap._host.set_safe_zone(64, 24)
            self.native.set_safe_zone.assert_called_once_with(64.0, 24.0)
            self.bootstrap.suspend()
            self.bootstrap.close()
        self.app.suspend.assert_called_once()
        self.app.resume.assert_called_once()
        self.app.close.assert_called_once()
        self.assertIsNone(self.bootstrap._app)
        self.assertIn("embedded CPython", "".join(self.output))

    def test_post_submission_hook_runs_only_when_host_notifies(self):
        self.app.presented = mock.Mock()
        with self.environment():
            self.bootstrap.initialize(self.config)
            self.bootstrap.frame({}, [])
            self.app.presented.assert_not_called()
            self.bootstrap.presented()
            self.app.presented.assert_called_once_with()
            self.app.presented.side_effect = RuntimeError("after-frame failure")
            with self.assertRaisesRegex(RuntimeError, "after-frame failure"):
                self.bootstrap.presented()
            self.bootstrap.close()
            self.bootstrap.presented()  # No work after the application closes.
        self.assertEqual(self.app.presented.call_count, 2)

    def test_clipboard_forwards_unicode_text_without_changing_it(self):
        with self.environment():
            self.bootstrap.initialize(self.config)
            host = self.bootstrap._host
            self.assertEqual(host.get_clipboard_text(), "cached paste \N{SNOWMAN}")
            copied = "Python source \N{GRINNING FACE}\nembedded\0null"
            host.set_clipboard_text(copied)
            self.native.set_clipboard_text.assert_called_once_with(copied)
            self.native.get_clipboard_text.assert_called_once_with()

    def test_callback_errors_propagate_for_native_traceback_reporting(self):
        self.app.frame.side_effect = ValueError("bad user frame")
        with self.environment():
            self.bootstrap.initialize(self.config)
            with self.assertRaisesRegex(ValueError, "bad user frame"):
                self.bootstrap.frame({}, [])

    def test_app_identity_is_not_an_editor_constant(self):
        self.config['app_id'] = 'another-melty-app'
        with self.environment():
            self.bootstrap.initialize(self.config)
            self.launch.assert_called_once_with(self.config, self.bootstrap._host)
            with self.assertRaisesRegex(RuntimeError, 'already initialized'):
                self.bootstrap.initialize(self.config)


class NativeInputTests(unittest.TestCase):
    def test_queue_order_bounds_cancellation_and_cross_thread_production(self):
        with tempfile.TemporaryDirectory() as output:
            executable = Path(output) / "input-queue-tests"
            subprocess.run([shutil.which("clang++") or shutil.which("c++"), "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-pthread", "-I", str(ROOT / "Host"), str(Path(__file__).with_name("input_queue.cpp")), "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
