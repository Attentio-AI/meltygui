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
import uuid
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


def list_devices(*, include_unpaired=False, details=False):
    """Return stable CoreDevice identifiers with the device's own display name."""
    result = []
    for device in device_command(['list', 'devices']).get('devices', []):
        properties = device.get('deviceProperties', {})
        hardware = device.get('hardwareProperties', {})
        if not properties or not hardware:
            raise ValueError('Unsupported Xcode device metadata; update the MeltyGUI device adapter.')
        if hardware.get('platform') not in ('iOS', 'iPadOS') or hardware.get('reality') == 'simulated':
            continue
        connection = device.get('connectionProperties', {})
        if not include_unpaired and connection.get('pairingState') != 'paired':
            continue
        identifier, name = device.get('identifier'), properties.get('name')
        if not identifier or not name:
            raise ValueError('Xcode returned an iOS device without an identifier or name.')
        item = {'id': identifier, 'name': name}
        if details:
            item.update(paired=connection.get('pairingState') == 'paired',
                        connected=connection.get('tunnelState') == 'connected',
                        transport=connection.get('transportType', 'unknown'),
                        developer_mode=properties.get('developerModeStatus', 'unknown'))
        result.append(item)
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


def installed_application_url(result, bundle_id):
    """Read devicectl's successful install result for the requested app."""
    matches = [app for app in result.get('installedApplications', [])
               if app.get('bundleID') == bundle_id]
    if len(matches) != 1 or not matches[0].get('installationURL'):
        raise ValueError(f'Xcode did not return an installation URL for {bundle_id}.')
    return matches[0]['installationURL']


def input_stamps(paths):
    """Track native/runtime inputs by path, mtime and size, without hashing files."""
    result = {}
    ignored = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', 'build', 'dist'}
    for value in paths:
        root = Path(value)
        if not root.exists():
            result[str(root)] = None
            continue
        if root.is_file():
            files = [root]
        else:
            files = []
            for directory, folders, names in os.walk(root):
                folders[:] = sorted(n for n in folders if n not in ignored and not n.endswith('.egg-info'))
                files.extend(Path(directory) / name for name in sorted(names) if not name.endswith('.pyc'))
        for path in files:
            stat = path.stat()
            result[str(path)] = [stat.st_mtime_ns, stat.st_size]
    return result


def native_inputs(build, config):
    project = build / 'MeltyIOS.xcodeproj/project.pbxproj'
    paths = [build / 'host-build.json', project, Path(__file__).parent]
    paths.extend(config[key] for key in ('python_lib', 'bootstrap_dir', 'packages_dir', 'shaders_dir') if config.get(key))
    if project.is_file():
        objects = plistlib.loads(project.read_bytes())['objects']
        paths.extend(item['path'] for item in objects.values()
                     if item.get('isa') == 'PBXFileReference' and Path(item.get('path', '')).is_absolute())
    if config.get('packages_dir'):
        manifest = Path(config['packages_dir']).parent / 'manifest.json'
        paths.append(manifest)
        if manifest.is_file():
            for item in json.loads(manifest.read_text())['packages']:
                paths.append(item.get('source') or item['wheel'])
    environment = xcode_environment()
    return {'files': input_stamps(paths), 'developer_dir': environment.get('DEVELOPER_DIR', ''), 'config': config}


def application_stamps(app_dir):
    return {p.relative_to(app_dir).as_posix(): [p.stat().st_mtime_ns, p.stat().st_size]
            for p in sorted(app_dir.rglob('*')) if p.is_file()}


def update_application(device, bundle_id, app_dir, receipt):
    """Publish app sources only; a commit descriptor follows a successful upload."""
    files = application_stamps(app_dir)
    # Native code cannot be loaded from a writable container.
    if any(Path(name).suffix in ('.so', '.dylib', '.fwork') for name in files):
        return False
    if files == receipt.get('app_files'):
        print('App sources unchanged; using installed app.', flush=True)
        return True
    destination = 'Library/Application Support/meltygui/app-update'
    generation = uuid.uuid4().hex
    common = ['--device', device, '--domain-type', 'appDataContainer', '--domain-identifier', bundle_id]
    print('Updating app sources on device…', flush=True)
    device_command(['device', 'copy', 'to', *common, '--source', str(app_dir),
                    '--destination', destination + '/' + generation + '/app'], timeout=120)
    with tempfile.TemporaryDirectory(prefix='melty-update-') as temporary:
        descriptor = Path(temporary) / 'update.json'
        descriptor.write_text(json.dumps({'base_generation': receipt['generation'],
                                          'generation': generation, 'files': files}))
        device_command(['device', 'copy', 'to', *common, '--source', str(descriptor),
                        '--destination', destination + '/update.json'], timeout=30)
    receipt['app_files'] = files
    return True


def run_application(request):
    from meltygui.platforms.ios.application import read_application
    from meltygui.platforms.ios.provision import ensure_configuration
    from meltygui.platforms.ios.stage_dependencies import copy_application, refresh_local_packages
    import fcntl

    root, build = Path(request['root']), Path(request['build'])
    device = request['device']
    config = json.loads((build / 'host-build.json').read_text(encoding='utf-8'))
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
        config, compiler, runtime = ensure_configuration(root, build, config)
        snapshot_file = build / 'run-source-snapshot.json'
        snapshot = None
        with tempfile.TemporaryDirectory(prefix='run-', dir=build) as temporary:
            staged = Path(temporary) / 'app'
            copy_application(application, staged)
            if request['text'] is not None:
                entry = (staged / request['module']).resolve()
                if not entry.is_relative_to(staged):
                    raise ValueError('The entry module escapes the staged application.')
                entry.write_text(request['text'], encoding='utf-8')
                previous_entry = app_dir / request['module']
                previous_snapshot = json.loads(snapshot_file.read_text()) if snapshot_file.is_file() else None
                snapshot = {'module': request['module'], 'text': request['text']}
                if previous_entry.is_file() and previous_snapshot:
                    stat = previous_entry.stat()
                    if (previous_snapshot.get('snapshot') == snapshot and
                            previous_snapshot.get('stamp') == [stat.st_mtime_ns, stat.st_size]):
                        os.utime(entry, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            if app_dir.exists():
                shutil.rmtree(app_dir)
            app_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), app_dir)
        if snapshot is not None:
            stat = (app_dir / request['module']).stat()
            snapshot_file.write_text(json.dumps({'snapshot': snapshot, 'stamp': [stat.st_mtime_ns, stat.st_size]}))
        elif snapshot_file.exists():
            snapshot_file.unlink()

        receipt_path = build / 'run-deployments.json'
        receipts = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
        receipt = receipts.get(device, {})
        python_inputs = dict(project_python=compiler, python_runtime=runtime)
        inputs = native_inputs(build, config) | python_inputs
        incremental = False
        if receipt.get('inputs') == inputs:
            installed = device_command(['device', 'info', 'apps', '--device', device])
            incremental = any(app.get('bundleIdentifier') == receipt['bundle_id'] and
                              app.get('url') == receipt['application_url']
                              for app in installed.get('apps', []))
        if incremental:
            incremental = update_application(device, receipt['bundle_id'], app_dir, receipt)
        if not incremental:
            if config.get('packages_dir'):
                print('Refreshing local iOS packages…', flush=True)
                refresh_local_packages(config['packages_dir'], python_version=runtime['full_version'])
            derived = build / 'run-products'
            print('Building iOS app…', flush=True)
            subprocess.run([xcrun(), 'xcodebuild', '-project', str(build / 'MeltyIOS.xcodeproj'),
                            '-scheme', 'Melty', '-configuration', 'Debug', '-destination', 'generic/platform=iOS',
                            '-derivedDataPath', str(derived), 'build'], check=True, close_fds=False, env=xcode_environment())
            bundle = derived / 'Build/Products/Debug-iphoneos/Melty.app'
            info = plistlib.loads((bundle / 'Info.plist').read_bytes())
            print('Installing on device…', flush=True)
            installed = device_command(['device', 'install', 'app', '--device', device, str(bundle)], timeout=120)
            application_url = installed_application_url(installed, info['CFBundleIdentifier'])
            settings_path = bundle / 'HostSettings.plist'
            settings = plistlib.loads(settings_path.read_bytes()) if settings_path.is_file() else {}
            receipt = dict(inputs=native_inputs(build, config) | python_inputs, bundle_id=info['CFBundleIdentifier'],
                           executable=info['CFBundleExecutable'], application_url=application_url,
                           generation=settings.get('source_generation'), app_files=application_stamps(app_dir))
            # Only a host with the writable update protocol can accept source-only runs.
            if settings.get('source_updates') == 1:
                receipts[device] = receipt
            else:
                receipts.pop(device, None)
        pending_receipt = receipt_path.with_suffix('.tmp')
        pending_receipt.write_text(json.dumps(receipts))
        pending_receipt.replace(receipt_path)
        application_url = receipt['application_url']
        previous = signal.getsignal(signal.SIGTERM)

        def stopped(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stopped)
        try:
            print('Running on device…', flush=True)
            subprocess.run([xcrun(), 'devicectl', 'device', 'process', 'launch', '--device', device,
                            '--terminate-existing', '--console', receipt['bundle_id']],
                           check=True, close_fds=False, env=xcode_environment())
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                terminate_app(device, application_url, receipt['executable'])
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
