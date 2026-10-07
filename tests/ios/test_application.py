"""Build inputs belong to an app; platform recipes select only its dependencies."""
from pathlib import Path
import zipfile

import pytest

from meltygui.platforms.ios.application import read_application
from meltygui.platforms.ios.stage_dependencies import copy_application, resolve_wheels


def wheel(root, name, requirements=()):
    path = root / f'{name}-1.0-py3-none-any.whl'
    metadata = '\n'.join(['Metadata-Version: 2.3', f'Name: {name}', 'Version: 1.0',
                          *[f'Requires-Dist: {requirement}' for requirement in requirements]])
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'{name}-1.0.dist-info/METADATA', metadata)
    return path


def test_second_app_does_not_install_pro_or_unused_recipes(tmp_path):
    toolkit = wheel(tmp_path, 'meltygui', ['core', 'desktop; sys_platform != "ios"'])
    core = wheel(tmp_path, 'core')
    pro = wheel(tmp_path, 'meltygui_pro', ['unavailable-ide-dependency'])
    selected = resolve_wheels(['meltygui'], [toolkit, core, pro],
                             [{'name': 'unused', 'url': 'must-not-download'}], tmp_path, offline=True)
    assert set(selected) == {toolkit, core}


def test_selects_wheels_and_markers_for_the_project_python(tmp_path):
    root = wheel(tmp_path, 'application', ['native', 'only314; python_version >= "3.14"'])
    native = wheel(tmp_path, 'native')
    cp313 = native.with_name('native-1.0-cp313-cp313-ios_17_0_arm64_iphoneos.whl')
    native.rename(cp313)
    cp314 = cp313.with_name('native-1.0-cp314-cp314-ios_17_0_arm64_iphoneos.whl')
    cp314.write_bytes(cp313.read_bytes())
    only314 = wheel(tmp_path, 'only314')
    wheels = [root, cp313, cp314, only314]
    assert set(resolve_wheels(['application'], wheels, [], tmp_path, python_version='3.13.11')) == {root, cp313}
    assert set(resolve_wheels(['application'], wheels, [], tmp_path, python_version='3.14.0')) == {root, cp314, only314}


def test_root_and_transitive_extras_are_resolved(tmp_path):
    root = wheel(tmp_path, 'application', ['child[feature]; extra == "full"'])
    child = wheel(tmp_path, 'child', ['feature; extra == "feature"'])
    feature = wheel(tmp_path, 'feature')
    assert set(resolve_wheels(['application[full]'], [root, child, feature], [], tmp_path)) == {
        root, child, feature}
    with pytest.raises(ValueError, match='No device wheel'):
        resolve_wheels(['application[full]'], [root, child], [], tmp_path)
    with pytest.raises(ValueError, match='conflicts'):
        resolve_wheels(['application>=2'], [root], [], tmp_path)


def test_stages_declared_package_and_resources_without_editor_conventions(tmp_path):
    root = tmp_path / 'project'
    package = root / 'counter'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'main.py').write_text('print("counter")')
    (root / 'data').mkdir()
    (root / 'data/theme.txt').write_text('blue')
    (root / 'data/build').mkdir()
    (root / 'data/build/template.py').write_text('# application template')
    (root / 'private.txt').write_text('not an app resource')
    (root / 'pyproject.toml').write_text('''[tool.melty.app]
entry = "counter/main.py"
bundle_id = "org.example.counter"
sources = ["counter"]
resources = ["data"]
dependencies = ["meltygui"]
''')
    config = read_application(root)
    assert config['entry_module'] == 'counter.main'
    target = tmp_path / 'staged'
    copy_application(config, target)
    assert (target / 'counter/main.py').is_file()
    assert (target / 'data/theme.txt').read_text() == 'blue'
    assert (target / 'data/build/template.py').is_file()
    assert not (target / 'private.txt').exists()
    assert config['dependencies'] == ['meltygui']


def test_application_resource_cannot_escape_or_recursively_include_output(tmp_path):
    (tmp_path / 'main.py').touch()
    config = read_application(tmp_path)
    config['resources'] = ['../outside']
    with pytest.raises(ValueError, match='relative'):
        copy_application(config, tmp_path / 'build/app')
    config['resources'] = ['.']
    with pytest.raises(ValueError, match='output directory'):
        copy_application(config, tmp_path / 'build/app')
