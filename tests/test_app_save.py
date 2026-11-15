"""`@save` (core/runtime/app_save.py): a class's attributes kept in a pickle of its own beside
its module, or written back into its own source through the Rust `Source`."""
import ast
import importlib.util
import os
import pickle
import subprocess
import sys
import threading
import tokenize

import pytest

import meltygui
from meltygui import save
from meltygui.core.runtime import app, app_save, app_session
from meltygui.core.windowing import glfw_utils

APP = 'save-test'


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    """No class is registered; the session a checkpoint writes lands under tmp_path."""
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setattr(app_save, '_kept', {})


@pytest.fixture
def live(monkeypatch):
    """A running app with an empty session, on this thread, with nothing else to save."""
    session = app_session.AppSession()
    monkeypatch.setattr(app, '_state', dict(app._state, failed=False, session=session, app_id=APP))
    monkeypatch.setattr(app, '_ROOTS', [])
    monkeypatch.setattr(glfw_utils, '_render_thread_id', threading.get_ident())
    monkeypatch.setattr(app, '_flush_pending_saves', lambda: None)
    return session


def tiles(path):
    """A saved class as an app defines it, kept in `path`."""
    @save(path)
    class Tiles:
        tree = {'id': 0, 'axis': 'x', 'cells': [{'id': 1}, {'id': 2}]}
        zoom = 1.0
        picked = None
        _drag = None

        class Dock:
            side = 'left'

        def count(self):
            return len(self.tree['cells'])

    return Tiles


def relaunch(path):
    """What the next run does: the class is decorated again, in a process that knows none."""
    app_save._kept.clear()
    return tiles(path)


def module(tmp_path, monkeypatch, text=None, name='look_settings'):
    """Import `text` as a module from a file of its own in tmp_path."""
    path = tmp_path / f'{name}.py'
    path.write_text(LOOK if text is None else text, encoding='utf-8')
    spec = importlib.util.spec_from_file_location(name, path)
    found = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, found)
    spec.loader.exec_module(found)
    return found, path


def files(folder):
    return sorted(entry.name for entry in folder.iterdir() if entry.is_file())


LAYOUT = '''from meltygui import save


@save
class Layout:
    zoom = 1.0


@save('panels.pkl')
class Panels:
    open = ['files']


start = Layout.zoom           # the saved value is there for the lines below the class
'''


# --- the pickle -------------------------------------------------------------------
def test_fields_are_the_public_plain_attributes_of_the_class_and_its_inner_classes(tmp_path):
    Tiles = tiles(tmp_path / 'Tiles.pkl')
    kept = app_save._kept[f'{__name__}.tiles.<locals>.Tiles']
    assert [label for _, _, label in kept.fields] == ['tree', 'zoom', 'picked', 'Dock.side']
    assert kept.cls is Tiles


def test_the_pickle_lives_beside_the_module_named_after_the_class_or_as_told(tmp_path, monkeypatch):
    found, _ = module(tmp_path, monkeypatch, LAYOUT, 'layout_app')
    assert found.start == 1.0
    assert files(tmp_path) == ['layout_app.py']        # nothing is written at launch
    found.Layout.zoom = 2.5
    found.Panels.open.append('search')
    assert app_save.write() is True
    assert files(tmp_path) == ['Layout.pkl', 'layout_app.py', 'panels.pkl']
    assert pickle.loads((tmp_path / 'Layout.pkl').read_bytes()) == {'zoom': 2.5}
    assert pickle.loads((tmp_path / 'panels.pkl').read_bytes()) == {'open': ['files', 'search']}

    app_save._kept.clear()                      # the next run
    again, _ = module(tmp_path, monkeypatch, LAYOUT, 'layout_app')
    assert again.start == 2.5                   # read where the class is decorated
    assert again.Panels.open == ['files', 'search']


def test_round_trip_restores_values_edited_in_place(tmp_path):
    path = tmp_path / 'kept' / 'Tiles.pkl'     # an absolute path, in a folder that is not there yet
    Tiles = tiles(path)
    tree = Tiles.tree
    tree['cells'][0]['size'] = 412.5            # what draw_tiles does: the dict it was given, in place
    tree['cells'].append({'id': 3, 'axis': 'y', 'cells': [{'id': 4}, {'id': 5}], 'sizes': [470.0, 804.0]})
    Tiles.zoom = 2.5
    Tiles.picked = ('a', 3)
    Tiles.Dock.side = 'right'
    Tiles._drag = 'transient'
    assert app_save.write() is True
    assert Tiles.tree is tree                   # saving leaves the live objects alone

    Again = relaunch(path)
    assert Again.tree == {'id': 0, 'axis': 'x', 'cells': [
        {'id': 1, 'size': 412.5}, {'id': 2},
        {'id': 3, 'axis': 'y', 'cells': [{'id': 4}, {'id': 5}], 'sizes': [470.0, 804.0]}]}
    assert (Again.zoom, Again.picked, Again.Dock.side) == (2.5, ('a', 3), 'right')
    assert Again._drag is None                  # a private name is never saved
    assert Again().count() == 3
    assert os.listdir(path.parent) == ['Tiles.pkl']     # no temporary file is left


def test_a_field_the_class_dropped_is_ignored_and_a_new_one_keeps_its_default(tmp_path):
    path = tmp_path / 'Panel.pkl'
    path.write_bytes(pickle.dumps({'width': 420, 'gone': True}))

    @save(path)
    class Panel:
        width = 300
        added = 'default'

    assert (Panel.width, Panel.added) == (420, 'default')
    assert not hasattr(Panel, 'gone')
    assert app_save.write() is True
    assert pickle.loads(path.read_bytes()) == {'width': 420, 'added': 'default'}


def test_the_file_holds_the_state_of_the_checkpoint_and_the_later_one_wins(tmp_path, live):
    path = tmp_path / 'Tiles.pkl'
    Tiles = tiles(path)
    Tiles.tree['cells'][0]['size'] = 0.25
    assert app.checkpoint() is True
    Tiles.tree['cells'][0]['size'] = 0.8        # after the save: not in the file
    assert relaunch(path).tree['cells'][0]['size'] == 0.25

    Tiles = relaunch(path)
    Tiles.zoom = 2.0
    app.checkpoint()
    Tiles.zoom = 3.0
    app.checkpoint()
    assert relaunch(path).zoom == 3.0


def test_an_unchanged_class_is_not_written_again(tmp_path):
    path = tmp_path / 'Tiles.pkl'
    tiles(path).zoom = 2.0
    app_save.write()
    stamp = os.stat(path).st_mtime_ns
    Tiles = relaunch(path)
    assert app_save.write() is True
    assert os.stat(path).st_mtime_ns == stamp
    Tiles.zoom = 3.0
    app_save.write()
    assert pickle.loads(path.read_bytes())['zoom'] == 3.0


def test_a_failed_write_or_a_failed_runtime_keeps_the_last_good_file(tmp_path, live, capsys):
    path = tmp_path / 'Tiles.pkl'
    Tiles = tiles(path)
    Tiles.zoom = 2.0
    assert app.checkpoint() is True
    good = path.read_bytes()
    Tiles.picked = threading.Lock()             # a value pickle cannot write
    assert app.checkpoint() is False
    assert 'not written (TypeError' in capsys.readouterr().err
    assert path.read_bytes() == good
    assert sorted(os.listdir(tmp_path)) == ['Tiles.pkl', 'state']       # no temporary file is left
    Tiles.picked = None
    Tiles.zoom = 99.0
    app._state['failed'] = True                 # a frame raised: nothing replaces what was saved
    assert app.checkpoint() is False
    assert path.read_bytes() == good
    app._state['failed'] = False
    # Where the file cannot be read or made (a regular file stands where its folder should be):
    # the app still starts, and the failed save is reported.
    (tmp_path / 'folder').write_text('not a directory')
    app_save._kept.clear()
    Tiles = tiles(tmp_path / 'folder' / 'Tiles.pkl')
    assert 'could not be read (NotADirectoryError' in capsys.readouterr().err
    Tiles.zoom = 5.0
    assert app_save.write() is False
    assert 'not written (' in capsys.readouterr().err


def test_a_file_that_does_not_load_is_moved_aside_and_the_app_starts(tmp_path, capsys):
    path = tmp_path / 'Tiles.pkl'
    for junk in (b'not a pickle', pickle.dumps(['a', 'list'])):
        path.write_bytes(junk)
        Tiles = relaunch(path)
        assert Tiles.zoom == 1.0
        said = capsys.readouterr().err
        assert 'did not load' in said and '.broken-' in said
        broken = [entry for entry in tmp_path.iterdir() if '.broken-' in entry.name]
        assert [entry.read_bytes() for entry in broken] == [junk]
        assert not path.exists()
        broken[0].unlink()
    Tiles.zoom = 2.0
    assert app_save.write() is True
    assert relaunch(path).zoom == 2.0


def test_a_hotswap_running_the_class_again_keeps_the_first_class_live(tmp_path):
    path = tmp_path / 'Tiles.pkl'
    first = tiles(path)
    first.zoom = 3.0
    second = tiles(path)                        # the same module and name: what a hotswap executes
    assert second is not first
    assert app_save._kept[f'{__name__}.tiles.<locals>.Tiles'].cls is first
    app_save.write()
    assert pickle.loads(path.read_bytes())['zoom'] == 3.0


def test_two_classes_cannot_share_a_file(tmp_path):
    tiles(tmp_path / 'Tiles.pkl')
    with pytest.raises(ValueError, match='Another class is saved in'):
        @save(tmp_path / 'Tiles.pkl')
        class Other:
            size = 1


def test_embedded_imports_read_and_write_nothing(tmp_path, monkeypatch):
    path = tmp_path / 'Layout.pkl'
    path.write_bytes(pickle.dumps({'zoom': 9.0}))
    stamp = os.stat(path).st_mtime_ns
    with meltygui.embedded_imports():
        found, _ = module(tmp_path, monkeypatch, LAYOUT, 'layout_app')
        found.Layout.zoom = 4.0                 # a preview may change the class it shows
        found.Panels.open.append('x')
    assert found.start == 1.0                   # the app's pickle was not read
    assert app_save._kept == {}
    assert app_save.write() is True
    assert pickle.loads(path.read_bytes()) == {'zoom': 9.0}
    assert os.stat(path).st_mtime_ns == stamp
    assert not (tmp_path / 'panels.pkl').exists()


def test_save_takes_a_class_with_a_file_to_live_beside(tmp_path):
    with pytest.raises(TypeError, match='decorates a class'):
        save('state.pkl')(lambda: None)
    with pytest.raises(TypeError, match='not into a pickle'):
        @save('look.pkl', to_code=True)
        class Both:
            size = 1
    with pytest.raises(TypeError, match='top of a file'):
        @save(to_code=True)
        class Local:
            size = 1
    homeless = type('Homeless', (), {'size': 1, '__module__': 'typed_at_a_prompt'})
    with pytest.raises(TypeError, match='has no file'):
        save(homeless)
    assert save(tmp_path / 'Homeless.pkl')(homeless) is homeless    # an absolute path needs no module


# --- the source -------------------------------------------------------------------
LOOK = '''"""A module with settings."""
from meltygui import save

BASE = 4


@save(to_code=True)
class Look:
    """Kept in this file."""
    width = 1232          # logical pixels
    height: int = 1252
    name = "gym"
    ratio = 2 / 3
    items = [1, 2,
             3]
    tree = {'id': 0, 'axis': 'x',
            'cells': [{'id': 1}, {'id': 2}]}
    tags = {'b', 'a'}
    codes = {1: 'one', 2: 'two'}
    blob = b'ab'
    flag = True; other = None
    if BASE:
        guarded = 1
    twice = 1
    twice = 2
    a = b = 3
    _private = 4

    class Editor:
        tab = 4

    def area(self):
        return self.width * self.height


after = Look.width  # é: text that is not ASCII stays as it is
'''


def line(path, start):
    return next(line.strip() for line in path.read_text(encoding='utf-8').split('\n') if line.strip().startswith(start))


def test_changed_values_are_written_over_their_defaults_and_nothing_else_moves(tmp_path, monkeypatch, capsys):
    found, path = module(tmp_path, monkeypatch)
    Look = found.Look
    Look.width = 1300
    Look.height = 900
    Look.name = 'it\'s "new"'
    Look.items.append(4)
    Look.tree['cells'][0]['size'] = 0.1 + 0.2
    Look.tags.add('z')
    Look.codes[3] = 'three'
    Look.blob = b'\x00c'
    Look.flag = False
    Look.other = 'now'
    Look.Editor.tab = 8
    Look._private = 9
    assert app_save.write() is True
    assert capsys.readouterr().err == ''

    expected = LOOK
    for old, new in [
            ('width = 1232  ', 'width = 1300  '), ('height: int = 1252', 'height: int = 900'),
            ('name = "gym"', 'name = \'it\\\'s "new"\''), ('items = [1, 2,\n             3]', 'items = [1, 2, 3, 4]'),
            ("tree = {'id': 0, 'axis': 'x',\n            'cells': [{'id': 1}, {'id': 2}]}",
             "tree = {'id': 0, 'axis': 'x', 'cells': [{'id': 1, 'size': 0.30000000000000004}, {'id': 2}]}"),
            ("tags = {'b', 'a'}", "tags = {'a', 'b', 'z'}"), ("codes = {1: 'one', 2: 'two'}", "codes = {1: 'one', 2: 'two', 3: 'three'}"),
            ("blob = b'ab'", "blob = b'\\x00c'"), ('flag = True; other = None', "flag = False; other = 'now'"),
            ('tab = 4', 'tab = 8')]:
        assert expected.count(old) == 1
        expected = expected.replace(old, new)
    assert path.read_text(encoding='utf-8') == expected

    # The next run starts from the file, with the values this one left.
    again, _ = module(tmp_path, monkeypatch, expected, 'look_again')
    for name in ('width', 'height', 'name', 'items', 'tree', 'tags', 'codes', 'blob', 'flag', 'other'):
        assert getattr(again.Look, name) == getattr(Look, name)
    assert again.Look.Editor.tab == 8
    assert again.Look._private == 4


def test_the_rust_tree_finds_the_span_and_no_python_parser_reads_the_file(tmp_path, monkeypatch):
    found, path = module(tmp_path, monkeypatch)
    from meltygui.model.source_model import Source

    def banned(*args, **kwargs):
        raise AssertionError('the source is read by the Rust parser only')

    splices = []
    splice = Source.splice

    def spy(self, low, high, new, *args, **kwargs):
        splices.append((self.native.slice(low, high), new))
        return splice(self, low, high, new, *args, **kwargs)

    found.Look.width = 640
    found.Look.Editor.tab = 2
    with monkeypatch.context() as patch:
        patch.setattr(Source, 'splice', spy)
        for parser in ('parse', 'literal_eval'):
            patch.setattr(ast, parser, banned)
        for parser in ('generate_tokens', 'tokenize', 'open', 'detect_encoding'):
            patch.setattr(tokenize, parser, banned)
        written = app_save.write()
    assert written is True
    assert splices == [('1232', '640'), ('4', '2')]
    assert line(path, 'width') == 'width = 640          # logical pixels'


def test_nothing_changed_reads_and_writes_nothing(tmp_path, monkeypatch):
    found, path = module(tmp_path, monkeypatch)
    before = os.stat(path).st_mtime_ns
    monkeypatch.setattr('builtins.open', lambda *args, **kwargs: pytest.fail('the file was opened'))
    assert app_save.write() is True
    assert os.stat(path).st_mtime_ns == before


def test_the_launch_does_not_load_the_source_extension(tmp_path):
    path = tmp_path / 'launch.py'
    path.write_text('import sys\nfrom meltygui import save\n\n\n@save(to_code=True)\nclass Look:\n    size = 1\n\n\n'
                    'print([name for name in sys.modules if "source" in name or "tree_model" in name])\n')
    result = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, close_fds=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '[]'


def test_a_field_that_is_no_plain_assignment_is_left_and_said(tmp_path, monkeypatch, capsys):
    found, path = module(tmp_path, monkeypatch)
    Look = found.Look
    Look.guarded = 2            # assigned under an `if`
    Look.twice = 3              # assigned twice
    Look.a = 5                  # one of two targets
    Look.ratio = 0.75           # an expression in the file
    Look.width = 1500           # a plain literal: written
    assert app_save.write() is True
    said = capsys.readouterr().err
    for name in ('guarded', 'twice', 'a'):
        assert f': {name} needs one plain assignment' in said
    assert 'ratio is `2 / 3` in the file, not the `0.6666666666666666` this run started from' in said
    text = path.read_text(encoding='utf-8')
    assert 'width = 1500 ' in text
    for kept in ('ratio = 2 / 3', 'guarded = 1', 'twice = 1\n    twice = 2', 'a = b = 3'):
        assert kept in text


def test_a_default_edited_while_the_app_ran_is_not_overwritten(tmp_path, monkeypatch, capsys):
    found, path = module(tmp_path, monkeypatch)
    Look = found.Look
    edited = LOOK.replace('width = 1232 ', 'width = 1000 ').replace('height: int = 1252', 'height: int = 800')
    path.write_text(edited, encoding='utf-8')
    Look.width = 1300           # the app changed it too: the file's edit stays
    Look.height = 800           # a hotswap brought the edit in: nothing to write
    Look.name = 'both'          # untouched in the file: written
    assert app_save.write() is True
    assert 'width is `1000` in the file, not the `1232`' in capsys.readouterr().err
    assert path.read_text(encoding='utf-8') == edited.replace('name = "gym"', "name = 'both'")
    # The file and the class agree on `height` now: a later change of it is written.
    Look.height = 820
    assert app_save.write() is True
    assert line(path, 'height') == 'height: int = 820'


def test_a_default_a_hotswap_brought_in_is_the_new_start(tmp_path, monkeypatch, capsys):
    found, path = module(tmp_path, monkeypatch)
    Look = found.Look
    edited = LOOK.replace('width = 1232 ', 'width = 1000 ')
    path.write_text(edited, encoding='utf-8')
    exec(compile(edited, str(path), 'exec'), {'__name__': 'look_settings'})     # the class body runs again
    assert app_save._kept['look_settings.Look'].cls is Look
    Look.width = 1000           # what the hotswap patches onto the live class
    assert app_save.write() is True
    assert path.read_text(encoding='utf-8') == edited
    Look.width = 1010           # the app moves on from the edited value: written over it
    assert app_save.write() is True
    assert capsys.readouterr().err == ''
    assert line(path, 'width') == 'width = 1010          # logical pixels'


def test_each_write_in_one_run_starts_from_the_last(tmp_path, monkeypatch):
    found, path = module(tmp_path, monkeypatch)
    Look = found.Look
    for width, size in [(1300, 0.1 + 0.2), (1400, 2 / 3), (1300, 0.5)]:
        Look.width = width
        Look.tree['cells'][1]['size'] = size
        Look.tags.add(str(width))
        assert app_save.write() is True
        assert line(path, 'width') == f'width = {width}          # logical pixels'
        assert repr(size) in line(path, 'tree')
    assert line(path, 'tags') == "tags = {'1300', '1400', 'a', 'b'}"
    compile(path.read_text(encoding='utf-8'), str(path), 'exec')


def test_a_value_without_a_literal_is_left_and_said(tmp_path, monkeypatch, capsys):
    found, path = module(tmp_path, monkeypatch)
    found.Look.other = object()
    found.Look.items.append(float('nan'))
    found.Look.width = 1
    assert app_save.write() is True
    said = capsys.readouterr().err
    assert 'other: <object object' in said and 'has no literal' in said
    assert 'items: nan has no literal' in said
    assert line(path, 'width').startswith('width = 1 ')
    assert 'other = None' in path.read_text(encoding='utf-8')
    with pytest.raises(TypeError, match='has no literal'):
        module(tmp_path, monkeypatch, 'from meltygui import save\n\n\n@save(to_code=True)\nclass Bad:\n    made = object()\n', 'bad')


def test_a_file_that_cannot_be_written_is_reported_and_left_whole(tmp_path, monkeypatch, capsys):
    found, path = module(tmp_path, monkeypatch)
    found.Look.width = 1300
    path.chmod(0o444)
    assert app_save.write() is False
    assert 'not written (PermissionError' in capsys.readouterr().err
    path.chmod(0o644)
    # A file that no longer compiles is not made worse.
    broken = LOOK + 'def (:\n'
    path.write_text(broken, encoding='utf-8')
    assert app_save.write() is False
    assert 'not written (SyntaxError' in capsys.readouterr().err
    assert path.read_text(encoding='utf-8') == broken
    assert sorted(entry.name for entry in tmp_path.iterdir() if entry.is_file()) == ['look_settings.py']


def test_a_write_keeps_the_file_mode_and_drops_the_compiled_copy(tmp_path, monkeypatch):
    found, path = module(tmp_path, monkeypatch)
    path.chmod(0o750)
    compiled = importlib.util.cache_from_source(str(path))
    os.makedirs(os.path.dirname(compiled), exist_ok=True)
    with open(compiled, 'wb') as file:
        file.write(b'stale')
    found.Look.width = 1300
    assert app_save.write() is True
    assert os.stat(path).st_mode & 0o7777 == 0o750
    assert not os.path.exists(compiled)


def test_a_long_file_is_read_on_the_writing_thread(tmp_path, monkeypatch):
    text = LOOK + ''.join(f'filler_{at} = {at}\n' for at in range(6000))
    assert len(text) > 60000                    # tree_model.INLINE: beyond it `source.tree` asks its thread
    found, path = module(tmp_path, monkeypatch, text, 'look_long')
    found.Look.width = 2000
    found.Look.Editor.tab = 2
    assert app_save.write() is True
    assert path.read_text(encoding='utf-8') == text.replace('width = 1232 ', 'width = 2000 ').replace('tab = 4', 'tab = 2')


def test_checkpoint_writes_the_source_and_the_pickle_together(tmp_path, monkeypatch, live):
    found, path = module(tmp_path, monkeypatch)
    Tiles = tiles(tmp_path / 'Tiles.pkl')
    Tiles.zoom = 2.0
    found.Look.width = 1111
    assert app.checkpoint() is True
    assert line(path, 'width').startswith('width = 1111 ')
    assert pickle.loads((tmp_path / 'Tiles.pkl').read_bytes())['zoom'] == 2.0
    assert live.app_state == {}                 # the session holds none of it
    # A failed runtime writes neither.
    Tiles.zoom = 3.0
    found.Look.width = 2222
    app._state['failed'] = True
    assert app.checkpoint() is False
    assert line(path, 'width').startswith('width = 1111 ')
    assert pickle.loads((tmp_path / 'Tiles.pkl').read_bytes())['zoom'] == 2.0
