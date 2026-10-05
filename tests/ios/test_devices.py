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
            return {'installationResults': [{'installationURL': application_url}]}
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
