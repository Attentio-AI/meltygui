"""Opt-in Mac display-server check: real drawables, not offscreen textures."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.environ.get('MELTY_METAL_TEST') != '1', reason='requires macOS display server and Xcode')
def test_display_link_drawables_complete_and_present(tmp_path):
    from meltygui.platforms.ios.devices import xcrun, xcode_environment
    source = Path(__file__).with_name('presentation.mm')
    host = source.parents[2] / 'meltygui/platforms/ios/Host'
    binary = tmp_path / 'presentation'
    env = dict(xcode_environment(), MTL_DEBUG_LAYER='1')
    subprocess.run([xcrun(), '--sdk', 'macosx', 'clang++', '-std=c++17', '-fobjc-arc',
                    '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                    '-I' + str(host), '-framework', 'Cocoa', '-framework', 'Metal',
                    '-framework', 'QuartzCore', str(source), '-o', str(binary)],
                   check=True, capture_output=True, text=True, close_fds=False, env=env)
    result = subprocess.run([str(binary)], capture_output=True, text=True, close_fds=False,
                            env=env, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'submitted=90 completed=90 presented=90' in result.stdout
