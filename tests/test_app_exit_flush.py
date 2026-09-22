"""The exit contract of app.run (core/runtime/app.py): the ``finally`` that
writes the pending saves, the session, the settings and the launch
overrides. Nothing else runs it, and a failure there is silent in a
desktop-launched app (stderr goes nowhere), so an edit that exits with a
queued save must be proven to land on disk here. The 2026-09-21 rewrite of
_flush_pending_saves left ``PendingSave`` unbound and every app with an
unsaved edit died in that block; these tests fail on that shape.

Run: .venv/bin/python -m pytest tests/test_app_exit_flush.py -q
"""
import os
import subprocess
import sys
import textwrap

import pytest

import meltygui.core.runtime.app as app
from meltygui.editor.pending_save import PendingSave
from meltygui.model.code_dict_model import CodeDict
from meltygui.model.code_dict_model import Codebase

from tests.test_code_dict_scenarios import SOURCE
from tests.test_code_dict_scenarios import disk_text
from tests.test_code_dict_scenarios import pending_text
from tests.test_code_dict_scenarios import project  # noqa: F401  fixture


def test_exit_flush_writes_a_queued_edit(project):  # noqa: F811
    """The function the finally block calls, with a real queued save: the
    edit reaches disk and the queue is empty afterwards."""
    CodeDict(project.Settings, write_to=Codebase)["speed"] = 2.25
    expected = pending_text(project)
    assert expected != SOURCE and PendingSave.pending_saves
    app._flush_pending_saves()
    assert disk_text(project) == expected
    assert not [a for a in PendingSave.pending_saves if a.path == project.__file__]


def test_exit_flush_does_not_load_the_code_stack(monkeypatch):
    """An app that never edited code has nothing queued: the flush must not
    import pending_save (libcst) on the way out."""
    monkeypatch.delitem(sys.modules, 'meltygui.editor.pending_save')
    app._flush_pending_saves()
    assert 'meltygui.editor.pending_save' not in sys.modules


APP = textwrap.dedent('''\
    import pathlib, sys
    import meltygui
    from meltygui.code.fileref import add_editable_root

    root = pathlib.Path(sys.argv[1])
    add_editable_root(root)
    sys.path.insert(0, str(root))

    @meltygui.glfw_window(name='Exit flush', app_id='exit-flush-test', width=320, height=200)
    def body():
        pass

    def queue_edit():
        from meltygui.model.code_dict_model import CodeDict, Codebase
        import cds_pkg.settings as settings
        CodeDict(settings.Settings, write_to=Codebase)["speed"] = 2.25

    meltygui.after_first_frame(queue_edit)
    meltygui.run()
    print('exit ok')
''')


@pytest.mark.skipif(not (os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')),
                    reason='needs a display: drives a real window through app.run')
def test_run_exits_through_the_flush_with_a_queued_edit(tmp_path):
    """The whole exit path: a window, a first frame, an edit queued from
    after_first_frame, MELTY_BENCH's exit after that frame. The finally
    block must write the edit, then the session and settings, and the
    process must exit cleanly (an exception in the block skips all of it
    and leaves a traceback nobody sees)."""
    package = tmp_path / 'cds_pkg'
    package.mkdir()
    (package / '__init__.py').write_text('PACKAGE_FLAG = 1\n')
    (package / 'settings.py').write_text(SOURCE)
    script = tmp_path / 'exit_app.py'
    script.write_text(APP)
    env = dict(os.environ, MELTY_BENCH='1',
               XDG_CACHE_HOME=str(tmp_path / 'cache'), XDG_STATE_HOME=str(tmp_path / 'state'),
               XDG_CONFIG_HOME=str(tmp_path / 'config'))
    result = subprocess.run([sys.executable, str(script), str(tmp_path)], env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert 'exit ok' in result.stdout
    text = (package / 'settings.py').read_text()
    assert 'speed = 2.25' in text and text != SOURCE
    assert (tmp_path / 'state' / 'exit-flush-test' / 'session.pkl').exists()
