"""Build hook: the Rust `Source` extension (native/source) is part of the package.

Every wheel build, and `uv pip install -e .` in a checkout, compiles it with
`tools/build_source.py` (cargo on PATH or in ~/.cargo/bin) against
Python's stable ABI and ships it as meltygui/model/_source_native.*. The
wheel is therefore built per platform (`cp311-abi3-<platform>`: one for every
CPython from 3.11 on), not `py3-none-any`.
"""
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from packaging.tags import sys_tags


class SourceBuild(BuildHookInterface):
    def initialize(self, version, build_data):
        subprocess.run([sys.executable, str(Path(self.root) / 'tools/build_source.py')], check=True, close_fds=False)
        build_data['pure_python'] = False
        build_data['tag'] = f'cp311-abi3-{next(sys_tags()).platform}'
        # The built library is ignored by git: name it so the wheel takes it.
        build_data['artifacts'].append('meltygui/model/_source_native*')
