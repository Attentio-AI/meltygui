"""The README's Python examples compile, and run to a first frame.

The smoke run opens real windows, so it only runs when asked for, from a
reserved agent desktop (docs/DEVELOPMENT.md):

    MELTY_README_SMOKE=1 .venv/bin/pytest tests/test_readme_examples.py
"""
import os
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest

README = pathlib.Path(__file__).resolve().parents[1] / 'README.md'
SMOKE_TIMEOUT_SECONDS = 120
# The first block is the complete script; its lines before the window call
# (imports and the volume) are what the later fragments build on.
WINDOW_CALL = 'meltygui.glfw_window('


def readme_blocks():
    return re.findall(r'```python\n(.*?)```', README.read_text(), re.S)


def runnable_examples():
    """Each README block as a script that stands alone: the first as written,
    a decorated window after the shared preamble, bare draw calls inside a
    decorated window body."""
    blocks = readme_blocks()
    preamble = blocks[0][:blocks[0].index(WINDOW_CALL)]
    examples = {}
    for index, block in enumerate(blocks):
        if index == 0:
            source = block
        elif '@meltygui.glfw_window' in block:
            source = preamble + block
        else:
            source = (preamble + f'@meltygui.glfw_window(name="Example {index}")\n'
                      'def example_window():\n' + textwrap.indent(block, '    '))
        examples[f'readme_block_{index}'] = source
    return examples


def test_readme_has_a_complete_first_example():
    blocks = readme_blocks()
    assert blocks and WINDOW_CALL in blocks[0]
    assert 'import meltygui' in blocks[0]


@pytest.mark.parametrize('name', sorted(runnable_examples()))
def test_readme_example_compiles(name):
    compile(runnable_examples()[name], name, 'exec')


@pytest.mark.skipif(not os.environ.get('MELTY_README_SMOKE'),
                    reason='opens windows; set MELTY_README_SMOKE=1 on an agent desktop')
@pytest.mark.parametrize('name', sorted(runnable_examples()))
def test_readme_example_draws_a_first_frame(name, tmp_path):
    script = tmp_path / f'{name}.py'
    script.write_text(runnable_examples()[name])
    env = dict(os.environ, MELTY_BENCH='1')
    # Separate session/save paths: the run never touches a real app's state.
    for variable in ('XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
        folder = tmp_path / variable.lower()
        folder.mkdir()
        env[variable] = str(folder)
    result = subprocess.run([sys.executable, str(script)], env=env, close_fds=False,
                            capture_output=True, text=True, timeout=SMOKE_TIMEOUT_SECONDS)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert 'Traceback' not in output, output
    assert 'window(s) created' in output, output
