"""Device command contracts; actual signing/transport require a Mac and iPhone."""
import json
from pathlib import Path
import plistlib

import pytest
from meltygui.platforms.ios import devices


def test_discovery_uses_device_names_and_stable_ids(monkeypatch):
    rows = [dict(identifier='device-a', deviceProperties={'name': 'Work iPhone'},
                 hardwareProperties={'platform': 'iOS'}, connectionProperties={'pairingState': 'paired'}),
            dict(identifier='device-b', deviceProperties={'name': 'Not paired'},
                 hardwareProperties={'platform': 'iOS'}, connectionProperties={'pairingState': 'unpaired'}),
            dict(identifier='mac', deviceProperties={'name': 'Mac'},
                 hardwareProperties={'platform': 'macOS'}, connectionProperties={'pairingState': 'paired'})]
    monkeypatch.setattr(devices, 'device_command', lambda args: {'devices': rows})
    assert devices.list_devices() == [{'id': 'device-a', 'name': 'Work iPhone'}]


def test_request_refuses_wrong_entry_and_missing_build(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, 'xcrun', lambda: '/usr/bin/xcrun')
    monkeypatch.delenv('MELTY_IOS_BUILD_DIR', raising=False)
    (tmp_path / 'main.py').write_text('print("app")')
    task = dict(module='main.py', cwd='.', env={})
    with pytest.raises(ValueError, match='Configure this app'):
        devices.execution_request(tmp_path, task, 'device-a')
    with pytest.raises(ValueError, match='select its Run task'):
        devices.execution_request(tmp_path, task | {'module': 'other.py'}, 'device-a')
    with pytest.raises(ValueError, match='not shell tasks'):
        devices.execution_request(tmp_path, {'cmd': 'echo hello'}, 'device-a')


def test_run_stages_snapshot_builds_installs_and_stops_exact_app(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, 'xcrun', lambda: '/usr/bin/xcrun')
    monkeypatch.setattr(devices, 'xcode_environment', lambda: {})
    monkeypatch.setattr(devices, 'list_devices', lambda: [{'id': 'device-a', 'name': 'Work iPhone'}])
    build = tmp_path / 'build/ios'
    build.mkdir(parents=True)
    source = tmp_path / 'main.py'
    source.write_text('print("saved")')
    (build / 'host-build.json').write_text(json.dumps(dict(
        app_dir=str(build / 'app-bundle/app'), entry_module='main')))
    bundle = build / 'run-products/Build/Products/Debug-iphoneos/Melty.app'
    bundle.mkdir(parents=True)
    (bundle / 'Info.plist').write_bytes(plistlib.dumps(dict(
        CFBundleIdentifier='org.example.test', CFBundleExecutable='Melty')))
    application_url = 'file:///device/Applications/unique/Melty.app/'
    calls = []

    def run(arguments, **kwargs):
        assert kwargs.get('close_fds') is False
        assert not any(key in kwargs for key in ('cwd', 'preexec_fn', 'start_new_session'))
        calls.append(arguments)
        if '--console' in arguments:
            raise KeyboardInterrupt

    def command(arguments, **kwargs):
        calls.append(arguments)
        if 'install' in arguments:
            return {'installedApplications': [{'bundleID': 'org.example.test',
                                               'installationURL': application_url}]}
        if 'processes' in arguments:
            return {'runningProcesses': [
                {'executable': application_url + 'Melty', 'processIdentifier': 42},
                {'executable': 'file:///device/Applications/other/Melty.app/Melty', 'processIdentifier': 99}]}
        return {}

    monkeypatch.setattr(devices.subprocess, 'run', run)
    monkeypatch.setattr(devices, 'device_command', command)
    request = dict(root=str(tmp_path), build=str(build), device='device-a', module='main.py', text='print("pending")')
    for _ in range(2):  # interruption also releases the per-build ownership lock
        with pytest.raises(KeyboardInterrupt):
            devices.run_application(request)
    assert source.read_text() == 'print("saved")'
    assert (build / 'app-bundle/app/main.py').read_text() == 'print("pending")'
    assert any('xcodebuild' in call for call in calls)
    terminated = [call[-1] for call in calls if 'terminate' in call]
    assert terminated == ['42', '42']


def test_xcode_environment_respects_explicit_directory(monkeypatch):
    monkeypatch.setenv('DEVELOPER_DIR', '/custom/Xcode/Contents/Developer')
    assert devices.xcode_environment()['DEVELOPER_DIR'] == '/custom/Xcode/Contents/Developer'


@pytest.mark.parametrize('selected_full_xcode', [False, True])
def test_xcode_environment_finds_beta_when_clt_selected(monkeypatch, selected_full_xcode):
    from types import SimpleNamespace
    monkeypatch.delenv('DEVELOPER_DIR', raising=False)
    selected = '/custom/Xcode/Contents/Developer' if selected_full_xcode else '/Library/Developer/CommandLineTools'
    monkeypatch.setattr(devices.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=selected))
    beta = Path('/Applications/Xcode-beta.app/Contents/Developer')
    monkeypatch.setattr(Path, 'glob', lambda *a: [beta.parent.parent])
    monkeypatch.setattr(Path, 'is_file', lambda path: path == beta / 'usr/bin/devicectl' or
                        (selected_full_xcode and path == Path(selected) / 'usr/bin/devicectl'))
    env = devices.xcode_environment()
    assert env.get('DEVELOPER_DIR') == (None if selected_full_xcode else str(beta))


def test_xcode_environment_reports_missing_full_xcode(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.delenv('DEVELOPER_DIR', raising=False)
    monkeypatch.setattr(devices.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='/Library/Developer/CommandLineTools'))
    monkeypatch.setattr(Path, 'glob', lambda *a: [])
    monkeypatch.setattr(Path, 'is_file', lambda path: False)
    with pytest.raises(ValueError, match='require full Xcode'):
        devices.xcode_environment()


def test_device_command_decodes_unicode_under_ascii_locale(tmp_path, monkeypatch):
    import subprocess
    import sys
    real_run = subprocess.run
    monkeypatch.setattr(devices, "xcrun", lambda: "/usr/bin/xcrun")
    monkeypatch.setattr(devices, "xcode_environment", lambda: {})
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "ascii")
    def run(arguments, **kwargs):
        output = Path(arguments[arguments.index("--json-output") + 1])
        output.write_bytes(json.dumps({"result": {"name": "Lukas’s iPhone"}},
                                     ensure_ascii=False).encode("utf-8"))
        kwargs.pop("env")
        return real_run([sys.executable, "-c",
                         "import sys; sys.stdout.buffer.write(bytes.fromhex('e28099'))"], **kwargs)
    monkeypatch.setattr(devices.subprocess, "run", run)
    assert devices.device_command(["list", "devices"]) == {"name": "Lukas’s iPhone"}


def test_install_result_selects_the_requested_bundle():
    result = {'installedApplications': [
        {'bundleID': 'other.app', 'installationURL': 'file:///other.app/'},
        {'bundleID': 'local.melty.codeeditor', 'installationURL': 'file:///Melty.app/'},
    ]}
    assert devices.installed_application_url(result, 'local.melty.codeeditor') == 'file:///Melty.app/'


@pytest.mark.parametrize('result', [{}, {'installedApplications': []},
    {'installedApplications': [{'bundleID': 'other.app', 'installationURL': 'file:///other.app/'}]},
    {'installedApplications': [{'bundleID': 'local.melty.codeeditor'}]},
])
def test_install_result_reports_missing_app_metadata(result):
    with pytest.raises(ValueError, match='installation URL for local.melty.codeeditor'):
        devices.installed_application_url(result, 'local.melty.codeeditor')


def test_setup_includes_unpaired_physical_devices_with_status(monkeypatch):
    entries = [dict(identifier='phone', deviceProperties={'name': 'Phone', 'developerModeStatus': 'enabled'},
                    hardwareProperties={'platform': 'iOS', 'reality': 'physical'},
                    connectionProperties={'pairingState': 'unpaired', 'tunnelState': 'connected',
                                          'transportType': 'localNetwork'}),
               dict(identifier='simulator', deviceProperties={'name': 'Simulator'},
                    hardwareProperties={'platform': 'iOS', 'reality': 'simulated'},
                    connectionProperties={'pairingState': 'paired'})]
    monkeypatch.setattr(devices, 'device_command', lambda args: {'devices': entries})
    assert devices.list_devices() == []
    assert devices.list_devices(include_unpaired=True, details=True) == [
        dict(id='phone', name='Phone', paired=False, connected=True,
             transport='localNetwork', developer_mode='enabled')]


def test_source_only_runs_skip_build_and_install_and_retry_failed_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, 'xcrun', lambda: '/usr/bin/xcrun')
    monkeypatch.setattr(devices, 'xcode_environment', lambda: {})
    monkeypatch.setattr(devices, 'list_devices', lambda: [{'id': 'phone'}])
    revision = [1]
    monkeypatch.setattr(devices, 'native_inputs', lambda *args: {'revision': revision[0]})
    monkeypatch.setattr(devices, 'terminate_app', lambda *args: None)
    build = tmp_path / 'build'
    build.mkdir()
    (tmp_path / 'main.py').write_text('value = 1\n')
    (build / 'host-build.json').write_text(json.dumps({'app_dir': str(build / 'app'), 'entry_module': 'main'}))
    bundle = build / 'run-products/Build/Products/Debug-iphoneos/Melty.app'
    bundle.mkdir(parents=True)
    (bundle / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'test.app', 'CFBundleExecutable': 'Melty'}))
    (bundle / 'HostSettings.plist').write_bytes(plistlib.dumps({'source_generation': 'a' * 32, 'source_updates': 1}))
    calls = []
    fail_copy = [False]
    def command(args, **kwargs):
        calls.append(args)
        if 'install' in args:
            return {'installedApplications': [{'bundleID': 'test.app', 'installationURL': 'file:///Melty.app/'}]}
        if 'apps' in args:
            return {'apps': [{'bundleIdentifier': 'test.app', 'url': 'file:///Melty.app/'}]}
        if 'copy' in args and fail_copy[0]:
            raise RuntimeError('Connection lost')
        return {}
    monkeypatch.setattr(devices, 'device_command', command)
    monkeypatch.setattr(devices.subprocess, 'run', lambda args, **kwargs: calls.append(args))
    request = dict(root=str(tmp_path), build=str(build), device='phone', module='main.py', text='value = 1\n')
    devices.run_application(request)
    assert sum('xcodebuild' in call for call in calls) == 1
    devices.run_application(request)
    assert not any('copy' in call for call in calls)
    request['text'] = 'value = 2\n'
    fail_copy[0] = True
    with pytest.raises(RuntimeError, match='Connection lost'):
        devices.run_application(request)
    before_retry = len(calls)
    fail_copy[0] = False
    devices.run_application(request)
    assert sum('copy' in call for call in calls[before_retry:]) == 2  # sources, then commit
    assert sum('xcodebuild' in call for call in calls) == 1
    assert sum('install' in call for call in calls) == 1
    assert not any('--remove-existing-content' in call for call in calls)
    before_noop = len(calls)
    devices.run_application(request)
    assert not any('copy' in call for call in calls[before_noop:])
    revision[0] += 1
    devices.run_application(request)
    assert sum('install' in call for call in calls) == 2


def test_native_stamps_detect_changes_and_deletions_without_build_outputs(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    module = source / 'module.py'
    module.write_text('a = 1')
    before = devices.input_stamps([source])
    (source / 'build').mkdir()
    (source / 'build/noise').write_text('build output')
    assert devices.input_stamps([source]) == before
    module.write_text('a = 100')
    assert devices.input_stamps([source]) != before
    module.unlink()
    assert devices.input_stamps([source]) == {}
