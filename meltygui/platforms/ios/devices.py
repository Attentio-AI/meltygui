"""Discover Apple devices and run an application's configured iOS build.

The worker is an ordinary child process. Build, install, console streaming and
device termination share its lifetime; no Apple process runs on the UI thread.
"""
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlparse


def xcrun():
    if sys.platform != 'darwin':
        raise ValueError('iOS device execution requires macOS and Xcode.')
    executable = shutil.which('xcrun')
    if not executable:
        raise ValueError('Install Xcode and select its developer directory to use iOS devices.')
    return executable


def xcode_environment():
    """Keep explicit/selected Xcode; find full Xcode when only CLT is selected."""
    env = dict(os.environ)
    if env.get('DEVELOPER_DIR'):
        return env
    selected = subprocess.run(['/usr/bin/xcode-select', '-p'], close_fds=False,
                              capture_output=True, text=True, encoding="utf-8", timeout=5)
    directory = Path(selected.stdout.strip())
    if selected.returncode == 0 and (directory / 'usr/bin/devicectl').is_file():
        return env
    candidates = [Path('/Applications/Xcode.app'),
                  *sorted(Path('/Applications').glob('Xcode*.app'))]
    for application in candidates:
        directory = application / 'Contents/Developer'
        if (directory / 'usr/bin/devicectl').is_file():
            env['DEVELOPER_DIR'] = str(directory)
            return env
    raise ValueError('iOS devices require full Xcode; install Xcode or set DEVELOPER_DIR.')


def device_command(arguments, *, timeout=30):
    """Read machine output separately from devicectl's human progress output."""
    with tempfile.TemporaryDirectory(prefix='melty-device-') as directory:
        output = Path(directory) / 'result.json'
        result = subprocess.run([xcrun(), 'devicectl', *arguments, '--json-output', str(output)],
                                close_fds=False, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                                env=xcode_environment())
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or 'Device command failed.')
        data = json.loads(output.read_text(encoding="utf-8"))
        if data.get('error'):
            raise RuntimeError(str(data['error']))
        return data.get('result', {})


def list_devices():
    """Return stable CoreDevice identifiers with the device's own display name."""
    result = []
    for device in device_command(['list', 'devices']).get('devices', []):
        properties = device.get('deviceProperties', {})
        hardware = device.get('hardwareProperties', {})
        if not properties or not hardware:
            raise ValueError('Unsupported Xcode device metadata; update the MeltyGUI device adapter.')
        if hardware.get('platform') not in ('iOS', 'iPadOS'):
            continue
        if device.get('connectionProperties', {}).get('pairingState') != 'paired':
            continue
        identifier, name = device.get('identifier'), properties.get('name')
        if not identifier or not name:
            raise ValueError('Xcode returned an iOS device without an identifier or name.')
        result.append({'id': identifier, 'name': name})
    return sorted(result, key=lambda device: (device['name'].casefold(), device['id']))


def execution_request(root, task, device, text=None):
    """Validate a task before passing immutable inputs to the device worker."""
    from meltygui.platforms.ios.application import read_application
    xcrun()
    application = read_application(root)
    if 'module' not in task:
        raise ValueError('iOS devices run Melty app entry modules, not shell tasks.')
    module = str(Path(task['module']).with_suffix('')).replace(os.sep, '.')
    if module != application['entry_module']:
        raise ValueError(f'This iOS app starts {application["entry_module"]}; select its Run task.')
    if task.get('env'):
        raise ValueError('Environment overrides are not supported by the iOS app host.')
    if Path(task.get('cwd', '.')) != Path('.'):
        raise ValueError('iOS apps use the device workspace; custom task working directories are unavailable.')
    build = Path(os.environ.get('MELTY_IOS_BUILD_DIR') or application.get('ios', {}).get('build_directory', 'build/ios'))
    build = (Path(root) / build).resolve()
    if not (build / 'MeltyIOS.xcodeproj/project.pbxproj').is_file() or not (build / 'host-build.json').is_file():
        raise ValueError('Configure this app with meltygui.platforms.ios generate before running on a device.')
    return {'root': str(Path(root).resolve()), 'build': str(build), 'device': device,
            'module': task['module'], 'text': text}


def terminate_app(device, application_url, executable):
    """Stop only the app installed by this run, matched by its complete path."""
    app_path = unquote(urlparse(application_url).path).rstrip('/')
    expected = str(Path(app_path) / executable)
    for process in device_command(['device', 'info', 'processes', '--device', device], timeout=2).get('runningProcesses', []):
        path = unquote(urlparse(process.get('executable', '')).path)
        if path == expected:
            device_command(['device', 'process', 'terminate', '--device', device,
                            '--pid', str(process['processIdentifier'])], timeout=2)


def run_application(request):
    from meltygui.platforms.ios.application import read_application
    from meltygui.platforms.ios.stage_dependencies import copy_application
    import fcntl

    root, build = Path(request['root']), Path(request['build'])
    device = request['device']
    config = json.loads((build / 'host-build.json').read_text())
    app_dir = Path(config['app_dir']).resolve()
    if app_dir == build or not app_dir.is_relative_to(build):
        raise ValueError('Generate the iOS project with its staged app directory inside the iOS build directory.')
    application = read_application(root)
    if config['entry_module'] != application['entry_module']:
        raise ValueError('The iOS build entry differs from the app metadata; regenerate the project.')
    if device not in {item['id'] for item in list_devices()}:
        raise ValueError('The selected iOS device is no longer paired or available.')

    # Hold the lock through the run: another Tasks tile cannot overwrite the
    # bundle or terminate this run by deploying the same app concurrently.
    with (build / '.run.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('This iOS app is already running from another execution. Stop it first.') from None
        with tempfile.TemporaryDirectory(prefix='run-', dir=build) as temporary:
            staged = Path(temporary) / 'app'
            copy_application(application, staged)
            if request['text'] is not None:
                entry = (staged / request['module']).resolve()
                if not entry.is_relative_to(staged):
                    raise ValueError('The entry module escapes the staged application.')
                entry.write_text(request['text'])
            if app_dir.exists():
                shutil.rmtree(app_dir)
            app_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), app_dir)

        derived = build / 'run-products'
        print('Building iOS app…', flush=True)
        subprocess.run([xcrun(), 'xcodebuild', '-project', str(build / 'MeltyIOS.xcodeproj'),
                        '-scheme', 'Melty', '-configuration', 'Debug', '-destination', 'generic/platform=iOS',
                        '-derivedDataPath', str(derived), 'build'], check=True, close_fds=False, env=xcode_environment())
        bundle = derived / 'Build/Products/Debug-iphoneos/Melty.app'
        info = plistlib.loads((bundle / 'Info.plist').read_bytes())
        print('Installing on device…', flush=True)
        installed = device_command(['device', 'install', 'app', '--device', device, str(bundle)], timeout=120)
        installation, = installed['installationResults']
        application_url = installation['installationURL']
        previous = signal.getsignal(signal.SIGTERM)

        def stopped(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stopped)
        try:
            print('Running on device…', flush=True)
            subprocess.run([xcrun(), 'devicectl', 'device', 'process', 'launch', '--device', device,
                            '--terminate-existing', '--console', info['CFBundleIdentifier']],
                           check=True, close_fds=False, env=xcode_environment())
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                terminate_app(device, application_url, info['CFBundleExecutable'])
            finally:
                signal.signal(signal.SIGTERM, previous)


def main():
    try:
        if sys.argv[1:] != ['run']:
            raise ValueError('Use this module through a Melty execution target.')
        run_application(json.load(sys.stdin))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'iOS run failed: {error}', file=sys.stderr, flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
