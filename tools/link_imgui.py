"""Run the imgui binding from its checkout in every environment that runs meltygui from disk.

    .venv/bin/python tools/link_imgui.py            # every project folder beside this checkout
    .venv/bin/python tools/link_imgui.py ../tile-view ../toggles

Each environment gets `meltygui-imgui` as an editable install of ../meltygui-imgui, so all of them
load the one extension built in that folder: a change to the binding is one rebuild there
(docs/DEVELOPMENT.md, "The imgui binding"), with no release and no reinstall. An environment that
has it that way already is left alone. A first link compiles the binding (about a minute each).
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT.parent / 'meltygui-imgui'


def installed(python, name):
    """The direct_url.json of `name` in the environment of `python`: {} from a registry, None when absent."""
    for found in python.parents[1].glob(f'lib/python*/site-packages/{name}-*.dist-info'):
        url = found / 'direct_url.json'
        return json.loads(url.read_text()) if url.exists() else {}
    return None


def link(project, uv):
    python = project / '.venv/bin/python'
    melty = installed(python, 'meltygui') if python.exists() else None
    if not melty or not melty.get('dir_info', {}).get('editable'):
        return None                                        # no meltygui from disk here
    binding = installed(python, 'meltygui_imgui') or {}
    if binding.get('url') == BINDING.as_uri() and binding.get('dir_info', {}).get('editable'):
        return 'linked already'
    done = subprocess.run([uv, 'pip', 'install', '--python', str(python), '-e', str(BINDING)],
                          capture_output=True, text=True)
    return 'linked' if done.returncode == 0 else 'FAILED: ' + done.stderr.strip().splitlines()[-1]


def main():
    uv = shutil.which('uv')
    if uv is None:
        raise SystemExit('uv must be on PATH')
    projects = [Path(path).resolve() for path in sys.argv[1:]] or sorted(ROOT.parent.iterdir())
    for project in projects:
        state = link(project, uv)
        if state:
            print(f'{project.name}: {state}', flush=True)


if __name__ == '__main__':
    main()
