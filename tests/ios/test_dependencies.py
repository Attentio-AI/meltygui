"""Dependency staging keeps real resources and requires a complete iOS graph."""
from pathlib import Path
import stat
import struct
import tempfile
import tomllib
import unittest
from unittest import mock
import zipfile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[2] / "meltygui/platforms/ios"


from meltygui.platforms.ios import prepare_bundle as bundle
from meltygui.platforms.ios import stage_dependencies as staging


def binary(platform=2, cpu=0x0100000C):
    command = struct.pack('<6I', 0x32, 24, platform, 17 << 16, 17 << 16, 0)
    return struct.pack('<8I', 0xFEEDFACF, cpu, 0, 6, 1, len(command), 0, 0) + command


class DependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packages = self.root / 'packages'
        self.packages.mkdir()

    def wheel(self, name='example', version='1.0', requirements=(), resources=None, extras=()):
        normalized = name.replace('-', '_')
        path = self.root / f'{normalized}-{version}-py3-none-any.whl'
        metadata = [f'Metadata-Version: 2.3', f'Name: {name}', f'Version: {version}',
                    'Requires-Python: >=3.11']
        metadata.extend(f'Provides-Extra: {extra}' for extra in extras)
        metadata.extend(f'Requires-Dist: {requirement}' for requirement in requirements)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(f'{normalized}-{version}.dist-info/METADATA', '\n'.join(metadata) + '\n')
            archive.writestr(f'{normalized}-{version}.dist-info/WHEEL',
                             'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            for member, data in (resources or {}).items():
                archive.writestr(member, data)
        return path

    def install(self, name, version='1.0', requirements=(), extras=()):
        wheel = self.wheel(name, version, requirements, extras=extras)
        return staging.install_wheel(wheel, self.packages)

    def test_installs_package_data_and_metadata_with_wheel_data_mapping(self):
        resources = {
            'example/__init__.py': b'VALUE = 1\n',
            'example/fonts/code.ttf': b'font resource',
            'example/templates/project.toml': b'[project]\n',
            'example-1.0.dist-info/licenses/LICENSE': b'package license',
            'example-1.0.data/purelib/namespace/portable.py': b'# portable\n',
            'example-1.0.data/platlib/namespace/_native.cpython-313-iphoneos.so': binary(),
            'example-1.0.data/scripts/command': b'not an application resource',
            'example-1.0.data/headers/api.h': b'not needed at runtime',
        }
        wheel = self.wheel(resources=resources)
        result = staging.install_wheel(wheel, self.packages)
        self.assertEqual(result, dict(name='example', version='1.0', wheel=str(wheel),
                                     sha256=staging.digest(wheel)))
        for member in ('example/__init__.py', 'example/fonts/code.ttf',
                       'example/templates/project.toml', 'example-1.0.dist-info/licenses/LICENSE'):
            self.assertEqual((self.packages / member).read_bytes(), resources[member])
        self.assertTrue((self.packages / 'example-1.0.dist-info/METADATA').is_file())
        self.assertTrue((self.packages / 'example-1.0.dist-info/WHEEL').is_file())
        self.assertEqual((self.packages / 'namespace/portable.py').read_bytes(), b'# portable\n')
        self.assertEqual((self.packages / 'namespace/_native.cpython-313-iphoneos.so').read_bytes(), binary())
        self.assertFalse((self.packages / 'example-1.0.data').exists())
        self.assertEqual(set(staging.validate_dependencies(self.packages)), {'example'})

    def test_rejects_desktop_simulator_and_wrong_cpu_extension_binaries(self):
        for name, contents in [('macos', binary(platform=1)), ('simulator', binary(platform=7)),
                               ('intel', binary(cpu=0x01000007)), ('linux', b'\x7fELF' + bytes(60))]:
            with self.subTest(platform=name):
                destination = self.root / name
                destination.mkdir()
                wheel = self.wheel(name, resources={f'{name}/_native.so': contents})
                with self.assertRaises(ValueError):
                    staging.install_wheel(wheel, destination)

    def test_rejects_paths_that_escape_or_execute_from_wheel_resources(self):
        bad = ['../outside.py', '/outside.py', 'example\\outside.py',
               'example/../../outside.py', 'startup.pth', 'example/native.dylib',
               'example/native.pyd', 'example/native.dll', 'example-1.0.data/purelib/../../outside.py']
        for position, member in enumerate(bad):
            with self.subTest(member=member):
                destination = self.root / f'bad-{position}'
                destination.mkdir()
                wheel = self.wheel(resources={member: b'rejected'})
                with self.assertRaisesRegex(ValueError, 'Unsafe wheel path|Unsupported wheel resource'):
                    staging.install_wheel(wheel, destination)
        self.assertFalse((self.root / 'outside.py').exists())

    def test_rejects_symlink_members_and_conflicting_package_resources(self):
        symlink = zipfile.ZipInfo('example/link.py')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        wheel = self.wheel(resources={symlink: b'../../outside.py'})
        with self.assertRaisesRegex(ValueError, 'Unsafe wheel path'):
            staging.install_wheel(wheel, self.packages)
        self.assertFalse((self.packages / 'example/link.py').exists())

        first = self.wheel('first', resources={'shared/resource.txt': b'original'})
        staging.install_wheel(first, self.packages)
        second = self.wheel('second', resources={'shared/resource.txt': b'replacement'})
        with self.assertRaisesRegex(ValueError, 'Conflicting package resource'):
            staging.install_wheel(second, self.packages)
        self.assertEqual((self.packages / 'shared/resource.txt').read_bytes(), b'original')

    def test_dependency_closure_rejects_absent_and_incompatible_transitive_packages(self):
        self.install('application', requirements=['middle>=2,<3'])
        self.install('middle', '2.1', requirements=['leaf>=4,<5'])
        with self.assertRaisesRegex(ValueError, 'middle requires leaf'):
            staging.validate_dependencies(self.packages)
        self.install('leaf', '3.9')
        with self.assertRaisesRegex(ValueError, 'middle requires leaf'):
            staging.validate_dependencies(self.packages)

    def test_dependency_names_normalize_and_compatible_cycles_terminate(self):
        self.install('application', requirements=['Shared_Package>=2,<3'])
        self.install('shared-package', '2.1', requirements=['application==1.0'])
        installed = staging.validate_dependencies(self.packages)
        self.assertEqual(set(installed), {'application', 'shared-package'})

    def test_rejects_a_distribution_requiring_a_newer_python(self):
        self.install('application')
        metadata = self.packages / 'application-1.0.dist-info/METADATA'
        metadata.write_text(metadata.read_text().replace('Requires-Python: >=3.11', 'Requires-Python: >=3.14'))
        with self.assertRaisesRegex(ValueError, 'application requires Python >=3.14'):
            staging.validate_dependencies(self.packages)

    def test_requested_extras_are_transitive_and_unrequested_extras_are_optional(self):
        self.install('application', requirements=['library[crypto]>=2'])
        self.install('library', '2.0', requirements=[
            'signer[fast]>=3; extra == "crypto"', 'unused-ui; extra == "desktop"'],
            extras=['crypto', 'desktop'])
        with self.assertRaisesRegex(ValueError, 'library requires signer'):
            staging.validate_dependencies(self.packages)
        self.install('signer', '3.0', requirements=['accelerator>=1; extra == "fast"'], extras=['fast'])
        with self.assertRaisesRegex(ValueError, 'signer requires accelerator'):
            staging.validate_dependencies(self.packages)
        self.install('accelerator')
        self.assertEqual(set(staging.validate_dependencies(self.packages)),
                         {'application', 'library', 'signer', 'accelerator'})

    def test_markers_use_ios_arm64_python313_not_the_build_host(self):
        self.install('application', requirements=[
            'desktop-gl; sys_platform != "ios"',
            'mac-only; platform_system == "Darwin"',
            'linux-only; sys_platform == "linux"',
            'intel-only; platform_machine == "x86_64"',
            'old-python; python_version < "3.13"',
            'device; sys_platform == "ios" and platform_system == "iOS" '
            'and platform_machine == "arm64" and python_version == "3.13"',
        ])
        # Even an Intel Linux build host must evaluate the target environment.
        host = default_environment() | dict(sys_platform='linux', platform_system='Linux',
                                            platform_machine='x86_64', python_version='3.12')
        with mock.patch.object(staging, 'default_environment', return_value=host):
            with self.assertRaisesRegex(ValueError, 'application requires device'):
                staging.validate_dependencies(self.packages)
            self.install('device')
            self.assertEqual(set(staging.validate_dependencies(self.packages)), {'application', 'device'})


class ProjectMarkerTests(unittest.TestCase):
    def test_toolkit_keeps_desktop_dependencies_on_macos_and_excludes_them_on_ios(self):
        toolkit = Path(__file__).resolve().parents[2] / 'pyproject.toml'
        project = tomllib.loads(toolkit.read_text())['project']
        requirements = [Requirement(text) for text in project['dependencies']]

        def selected(platform, system):
            env = default_environment() | dict(sys_platform=platform, platform_system=system,
                                                platform_machine='arm64', python_version='3.13', extra='')
            return {canonicalize_name(requirement.name) for requirement in requirements
                    if requirement.marker is None or requirement.marker.evaluate(env)}

        desktop = {'glfw', 'pyopengl', 'pyopengl-accelerate', 'psutil'}
        shared_native = {'meltygui-imgui', 'numpy', 'libcst', 'freetype-py', 'pillow', 'rtree'}
        macos = selected('darwin', 'Darwin')
        ios = selected('ios', 'iOS')
        self.assertTrue(desktop <= macos)
        self.assertFalse(desktop & ios)
        self.assertTrue(shared_native <= ios)
        self.assertEqual(macos - ios, desktop)


if __name__ == '__main__':
    unittest.main()
