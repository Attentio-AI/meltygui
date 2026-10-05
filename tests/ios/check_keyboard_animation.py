#!/usr/bin/env python3
"""Run UIKit keyboard geometry checks in a supplied, reserved ARM64 simulator.

Uses production App.mm with a small Metal host, without Python dependencies.
The simulator must already be booted; this does not touch the editor's app data.
"""
import argparse
import json
from pathlib import Path
from meltygui.platforms.ios import build_directory
import plistlib
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2] / "meltygui/platforms/ios"
BUNDLE = 'local.melty.keyboardcheck'


def run(*args):
    return subprocess.check_output(list(map(str, args)), text=True, close_fds=False).strip()


def check(samples):
    active = [sample for sample in samples if sample['phase'] > 0]
    moving = [sample for sample in active if abs(sample['target_height']-sample['visible_height']) > .5]
    stretch = max(abs(sample['presented_height']*sample['scale']/sample['drawable_height']-1)
                  for sample in active)
    report = dict(samples=len(active), animated_samples=len(moving),
                  maximum_texture_stretch=stretch,
                  visible_heights=[min(s['visible_height'] for s in active),
                                   max(s['visible_height'] for s in active)],
                  canvas_heights=sorted({s['canvas_height'] for s in active}))
    assert len(moving) >= 5, report
    assert report['visible_heights'][1]-report['visible_heights'][0] > 100, report
    assert len(report['canvas_heights']) == 1, report
    assert stretch < .000001, report
    assert all(sample['visible_edge_receives_touch'] for sample in active), 'Visible content stopped accepting touches during animation'
    assert abs(active[-1]['visible_height']-active[0]['canvas_height']) < .5, report
    report['status'] = 'passed'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--simulator', required=True)
    args = parser.parse_args()
    output = build_directory() / 'keyboard-animation'
    bundle = output / 'After.app'
    bundle.mkdir(parents=True, exist_ok=True)
    sdk = run('/usr/bin/xcrun', '--sdk', 'iphonesimulator', '--show-sdk-path')
    compiler = run('/usr/bin/xcrun', '--sdk', 'iphonesimulator', '--find', 'clang++')
    info = plistlib.loads((ROOT / 'Host/Info.plist').read_bytes())
    info.update(CFBundleExecutable='KeyboardCheck', CFBundleIdentifier=BUNDLE,
                CFBundleName='KeyboardCheck', CFBundleDisplayName='Keyboard Check', MinimumOSVersion='17.0')
    (bundle / 'Info.plist').write_bytes(plistlib.dumps(info))
    run(compiler, '-std=c++17', '-target', 'arm64-apple-ios17.0-simulator',
        '-isysroot', sdk, '-fobjc-arc', '-framework', 'UIKit', '-framework', 'Foundation',
        '-framework', 'CoreGraphics', '-framework', 'Metal', '-framework', 'QuartzCore',
        '-I', ROOT / 'Host', ROOT / 'Host/App.mm', Path(__file__).with_name('keyboard_animation.mm'), '-o', bundle / 'KeyboardCheck')
    run('/usr/bin/codesign', '--force', '--sign', '-', bundle)
    run('/usr/bin/xcrun', 'simctl', 'install', args.simulator, bundle)
    container = Path(run('/usr/bin/xcrun', 'simctl', 'get_app_container', args.simulator, BUNDLE, 'data'))
    result = container / 'Documents/keyboard-geometry.json'
    result.unlink(missing_ok=True)
    run('/usr/bin/xcrun', 'simctl', 'launch', '--terminate-running-process', args.simulator, BUNDLE)
    deadline = time.monotonic() + 40
    while not result.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('The UIKit probe did not produce its geometry report')
        time.sleep(.1)
    samples = json.loads(result.read_text())
    (output / 'after-geometry.json').write_text(json.dumps(samples, indent=2))
    report = check(samples)
    (output / 'result.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
