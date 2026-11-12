"""The project roster (native/source/src/roster.rs, model/roster_model.py): the
tables of a project's files, resolution and uses across them, what the editor
takes from it (tints, lookup) and the events that keep it current.

`symbol_roster.resolve` is the oracle for resolution on the files of this
checkout, where its own tables are right: it reads lines by regex, so an
"entry" or an "import" it finds in a string or inside brackets is none.
"""
import bisect
import os
import threading
import time
import types
from pathlib import Path

import pytest

from meltygui.model.roster_model import Roster
from meltygui.model.source_model import Source

ROOT = Path(__file__).resolve().parents[1]
SKY, GROUND, DEPTH, CIRCLE = (0.1, 0.2, 0.3), (0.9, 0.5, 0.1), (0.2, 0.9, 0.2), (0.3, 0.3, 0.9)
FILES = {
    'palette.py': '''def tinted(tint):
    return lambda function: function


@tinted(tint=(0.1, 0.2, 0.3))
def sky(height):
    return height


# [tint=(0.9, 0.5, 0.1)]
class Ground:
    depth = 3  # [tint=(0.2, 0.9, 0.2)]

    def dig(self):
        return self.depth + self.shovel()

    def shovel(self):
        return 1


plain = 1
''',
    'pkg/__init__.py': 'from .shapes import Circle\nfrom . import shapes\n',
    'pkg/shapes.py': '''from palette import Ground as Soil
import palette


class Circle:  # [tint=(0.3, 0.3, 0.9)]
    def area(self):
        return palette.sky(Soil.depth)
''',
    'pkg/sub/__init__.py': '',
    'pkg/sub/deep.py': '''from ..shapes import Circle
from .. import shapes
from .... import nothing


def far():
    return Circle().area(), shapes.Circle, nothing
''',
    # (`import EVERYTHING`: so that no line of this file is a star import to the line-regex oracle test_source.py compares tables with.)
    'app.py': '''import pkg.shapes as forms
import pkg.shapes
from pkg import Circle
from palette import EVERYTHING
from pkg import shapes as s2


def main(plain):
    ring = Circle()
    print(f'{sky(2)} {ring.area()}')
    return forms.Circle, pkg.shapes.Circle, Ground.depth, s2.Circle, plain, lonely()
''',
    'solo.py': 'def lonely():\n    return 1\n',
}
FILES['app.py'] = FILES['app.py'].replace('import EVERYTHING', 'import *')


def wait(done, what='it'):
    deadline = time.monotonic() + 20
    while not done() and time.monotonic() < deadline:
        time.sleep(.01)
    assert done(), what


def place(text, word, nth=0):
    """(line, column) of the `nth` occurrence of `word` in `text`."""
    at = -1
    for _ in range(nth + 1):
        at = text.index(word, at + 1)
    return text.count('\n', 0, at), at - text.rfind('\n', 0, at) - 1


def opened(path, text=None):
    """A `Source` on `path` with one reading landed."""
    source = Source(path.read_text() if text is None else text, str(path))
    source.ask(now=True)
    assert source.land()
    return source


@pytest.fixture
def made(tmp_path):
    """(the roster of a small project read from disk, its folder)."""
    folder = Path(os.path.realpath(tmp_path)) / 'project'
    for name, text in FILES.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
    roster = Roster(folder, paths=[str(folder / name) for name in FILES], roots=[str(folder)])
    roster.settle()
    yield roster, folder
    roster.close()


def test_the_tables_are_read_cold_and_asked_by_line_and_by_name(made):
    roster, folder = made
    assert roster.native.counts()[:3] == (7, 7, 12), 'every file listed is read on the thread: 7 tables, 12 entries'
    palette = str(folder / 'palette.py')
    rows = roster.entries(palette, 9, 12)
    assert [row[:8] for row in rows] == [('Ground', 'Ground', 'class', 10, 19, 0, 6, -1), ('Ground.depth', 'depth', 'var', 11, 11, 4, 4, 2)]
    assert rows[0][9] == list(GROUND) and rows[1][8] == '    depth = 3  # [tint=(0.2, 0.9, 0.2)]'
    assert roster.named('shovel') == [(palette, 'Ground.shovel', 'def', 16, 8, [])]
    assert roster.named('Circle') == [(str(folder / 'pkg/shapes.py'), 'Circle', 'class', 4, 6, list(CIRCLE))] and roster.named('nowhere') == []
    assert roster.generation(palette) > 0 and roster.generation(str(folder / 'missing.py')) == 0


def test_a_chain_resolves_through_the_imports_of_its_file(made):
    roster, folder = made
    file = lambda name: str(folder / name)
    found = lambda name, chain, scope=None, guess=True: (roster.resolve(file(name), chain, scope, guess) or (None, None))[:2]
    circle, ground = (file('pkg/shapes.py'), 'Circle'), (file('palette.py'), 'Ground')
    assert found('app.py', 'forms.Circle') == circle, 'import x.y as z'
    assert found('app.py', 'pkg.shapes.Circle') == circle, 'import x.y: the longest module the chain spells'
    assert found('app.py', 'pkg.shapes') == (None, None), 'a module is no symbol'
    assert found('app.py', 'Circle') == circle and found('app.py', 'Circle.area') == (circle[0], 'Circle.area'), 'a re-export through the package __init__'
    assert found('app.py', 's2.Circle') == circle, 'from package import module as z'
    assert found('app.py', 'sky') == (file('palette.py'), 'sky') and found('app.py', 'Ground.depth') == (ground[0], 'Ground.depth'), 'a star import'
    assert found('pkg/shapes.py', 'Soil') == ground and found('pkg/shapes.py', 'Soil.depth') == (ground[0], 'Ground.depth'), 'from x import y as z'
    assert found('pkg/shapes.py', 'palette.sky') == (file('palette.py'), 'sky') and found('pkg/shapes.py', 'self.area', 'Circle.area') == (circle[0], 'Circle.area')
    assert found('pkg/sub/deep.py', 'Circle') == circle and found('pkg/sub/deep.py', 'shapes.Circle') == circle, 'relative imports, from the file\'s package'
    assert found('pkg/sub/deep.py', 'nothing') == (None, None), 'more dots than the package is deep'
    assert found('app.py', 'lonely') == (file('solo.py'), 'lonely') and found('app.py', 'lonely', guess=False) == (None, None), 'the guess: the one definition of a name nothing brings'
    assert found('solo.py', 'depth') == (None, None) and found('app.py', 'Ground.missing') == (None, None)
    assert roster.resolve(file('app.py'), 'Circle') == (circle[0], 'Circle', 4, 6), 'the definition\'s line and column come with it'


def test_the_uses_of_a_definition_are_found_across_the_project(made):
    roster, folder = made
    file = lambda name: str(folder / name)
    uses = lambda name, entry: sorted((path[len(str(folder)) + 1:], line, column, guess) for path, line, column, _, guess in roster.uses(file(name), entry))
    assert uses('pkg/shapes.py', 'Circle') == [
        ('app.py', 2, 16, False), ('app.py', 8, 11, False), ('app.py', 10, 17, False), ('app.py', 10, 36, False), ('app.py', 10, 61, False),
        ('pkg/__init__.py', 0, 20, False), ('pkg/sub/deep.py', 0, 21, False), ('pkg/sub/deep.py', 6, 11, False), ('pkg/sub/deep.py', 6, 35, False),
    ], 'import statements, calls, members of modules; every file by its own imports'
    assert uses('palette.py', 'sky') == [('app.py', 9, 13, False), ('pkg/shapes.py', 6, 23, False)], 'a field of an f-string is code'
    assert uses('palette.py', 'Ground.shovel') == [('palette.py', 14, 33, False)], 'self.name in a method is the class\'s'
    assert uses('palette.py', 'Ground.depth') == [('app.py', 10, 51, False), ('palette.py', 14, 20, False), ('pkg/shapes.py', 6, 32, False)]
    assert uses('pkg/shapes.py', 'Circle.area') == [('app.py', 9, 27, True)], 'a member reached through a value is a guess, while no other member has the name'
    assert uses('palette.py', 'plain') == [], 'a parameter of the same name is no use'
    assert roster.uses(file('palette.py'), 'missing') is None


def test_a_pushed_text_replaces_the_disk_and_moves_the_generation_only_for_what_others_see(made):
    roster, folder = made
    palette, app = str(folder / 'palette.py'), str(folder / 'app.py')
    text, first = FILES['palette.py'], roster.generation(palette)
    roster.push(palette, text.replace('def sky(height):', 'def sky(height):\n    height += 1'), 5)
    roster.settle()
    assert roster.generation(palette) == first and roster.resolve(app, 'Ground')[2] == 11, 'lines moved: asked again they are right, nobody is told'
    roster.push(palette, text.replace('(0.1, 0.2, 0.3)', '(0.7, 0.7, 0.7)'), 6)
    roster.settle()
    second = roster.generation(palette)
    assert second > first and roster.named('sky')[0][5] == [0.7, 0.7, 0.7], 'a tint is what others see'
    roster.push(palette, text, 4)
    roster.settle()
    assert roster.generation(palette) == second and roster.named('sky')[0][5] == [0.7, 0.7, 0.7], 'an older generation than the last told is dropped'
    roster.push(palette, text.replace('def sky', 'def cloud'), 7)
    roster.settle()
    assert roster.generation(palette) > second and roster.resolve(app, 'sky') is None and roster.resolve(app, 'cloud')[1] == 'cloud'
    fresh = str(folder / 'fresh.py')
    roster.push(fresh, 'def lonely():\n    pass\n', 1)
    roster.settle()
    assert roster.native.counts()[0] == 8 and roster.resolve(app, 'lonely') is None, 'a file pushed joins the project: the name is no longer one of a kind'


def test_an_open_source_is_its_files_table_and_looks_up_across_files(made):
    roster, folder = made
    file = lambda name: str(folder / name)
    app, shapes = opened(folder / 'app.py'), opened(folder / 'pkg/shapes.py')
    roster.join(app), roster.join(shapes)
    roster.settle()
    text = FILES['app.py']
    assert roster.lookup(app, *place(text, 'Circle()')) == (False, [(file('pkg/shapes.py'), 4, 6, 6, False)])
    assert roster.lookup(app, *place(text, 'depth')) == (False, [(file('palette.py'), 11, 4, 5, False)]), 'the part under the caret, not the chain'
    assert roster.lookup(app, *place(text, 'lonely')) == (False, [(file('solo.py'), 0, 4, 6, True)]), 'a guess says so'
    assert roster.lookup(app, *place(text, 'plain', 1)) is None and roster.lookup(app, 0, 0) is None, 'a parameter is the buffer\'s own business'
    found = roster.lookup(shapes, *place(FILES['pkg/shapes.py'], 'Circle'))
    assert found[0] and (file('app.py'), 8, 11, 6, False) in found[1] and len(found[1]) == 9, 'on a definition: its uses'
    # The buffer is edited and not saved: what it shows is what the other files see.
    at = FILES['pkg/shapes.py'].index('class Circle')
    shapes.splice(at, at, 'def square():  # [tint=(0.5, 0.5, 0.5)]\n    pass\n\n\n')
    shapes.ask(now=True)
    assert shapes.land()
    roster.settle()
    assert roster.lookup(app, *place(text, 'Circle()')) == (False, [(file('pkg/shapes.py'), 8, 6, 6, False)]), 'the unsaved lines'
    assert roster.resolve(file('app.py'), 'forms.square')[1:] == ('square', 4, 4) and roster.named('square')[0][5] == [0.5, 0.5, 0.5]
    # A text pushed for the file waits behind the open source; when the source leaves, the disk is the table again.
    roster.push(file('pkg/shapes.py'), 'def other():\n    pass\n', 9)
    roster.settle()
    assert roster.named('other') == [] and roster.named('square') != []
    roster.native.leave(file('pkg/shapes.py'))
    roster.settle()
    assert roster.named('square') == [] and roster.resolve(file('app.py'), 'forms.Circle')[2] == 4
    app.close(), shapes.close()


def test_a_buffer_the_roster_has_another_reading_of_finds_its_uses_in_other_files(made):
    """A source that never joined, or whose last reading the roster's thread has not installed yet, is
    asked by its own table: the other files' chains lead to the table the roster has of the file, and
    the entry there of the same dotted name is the buffer's (the usage picker asks right after an edit)."""
    roster, folder = made
    shapes, text = opened(folder / 'pkg/shapes.py'), FILES['pkg/shapes.py']
    whole = roster.uses(str(folder / 'pkg/shapes.py'), 'Circle')
    assert roster.lookup(shapes, *place(text, 'Circle')) == (True, whole) and len(whole) == 9
    at = text.index('class Circle')
    shapes.splice(at, at, 'def square():\n    pass\n\n\n')
    shapes.ask(now=True)
    assert shapes.land()
    assert roster.lookup(shapes, 8, 6) == (True, whole), 'the buffer\'s lines moved: the other files still lead to its entry'
    assert roster.lookup(shapes, 4, 4) == (True, []), 'an entry only the buffer has is used nowhere else'
    shapes.close()


def test_a_buffer_washes_what_it_uses_of_other_files_tints(made):
    roster, folder = made
    app = opened(folder / 'app.py')
    roster.join(app)
    lines = FILES['app.py'].split('\n')
    rows, needs = roster.tints(app)
    assert [(line, lines[line][column:column + chars], (red, green, blue), scale) for line, column, chars, red, green, blue, scale in rows] == [
        (2, 'Circle', CIRCLE, 1.), (8, 'Circle', CIRCLE, 1.), (9, 'sky', SKY, 1.),
        (10, 'Circle', CIRCLE, 1.), (10, 'Circle', CIRCLE, 1.), (10, 'Ground', GROUND, 1.), (10, 'depth', DEPTH, 1.), (10, 'Circle', CIRCLE, 1.),
    ], 'each part of a chain that names a tinted entry of another file, in line order; nothing for what the buffer binds itself'
    assert app.native.tints() == [], 'the buffer states no tint of its own: the two sets of rows never overlap'
    assert not roster.stale(needs)
    # Another file's tint changes: the answer is stale; a file the answer never looked at changes: it is not.
    roster.push(str(folder / 'pkg/sub/deep.py'), 'def far():  # [tint=(1.0, 1.0, 1.0)]\n    pass\n', 1)
    roster.settle()
    assert not roster.stale(needs), 'nothing the buffer uses'
    roster.push(str(folder / 'palette.py'), FILES['palette.py'].replace('(0.1, 0.2, 0.3)', '(0.6, 0.6, 0.6)'), 1)
    roster.settle()
    assert roster.stale(needs)
    rows, needs = roster.tints(app)
    assert (9, 13, 3, 0.6, 0.6, 0.6, 1.) in rows and not roster.stale(needs)
    # A name nothing brought gets a definition somewhere: the guess the answer made is stale too.
    roster.push(str(folder / 'late.py'), '@tinted(tint=(0.4, 0.4, 0.4))\ndef lonely():\n    pass\n', 1)
    roster.settle()
    assert roster.stale(needs) and not any(row[0] == 10 and row[3] == 0.4 for row in roster.tints(app)[0]), 'two definitions of the name: no guess'
    app.close()


def test_analysis_shows_the_tints_of_other_files_and_follows_them(made):
    from meltygui.editor.marks import Mark
    from meltygui.model import editor_model
    roster, folder = made
    text = FILES['app.py'].replace('def main(plain):', '# [tint=(0.5, 0.1, 0.1)]\ndef main(plain):')
    app = opened(folder / 'app.py', text)
    roster.join(app)
    analysis = editor_model.Analysis(app, roster)
    try:
        wait(lambda: analysis.tints is not None, 'the worker makes the marks')
        assert analysis.tints.on(8, 8) == [(8, Mark('tint', 0, None, (0.5, 0.1, 0.1)))], 'the buffer\'s own definition tint'
        assert analysis.tints.on(9, 10) == [(9, Mark('wash', 11, 17, CIRCLE, 1.)), (10, Mark('wash', 13, 16, SKY, 1.))], 'and the other files\', one set in order'
        assert analysis.tints.version == app.version
        # The definition's tint changes in a pushed text: the buffer follows without a reading of its own.
        landed, marks = app.stamp, analysis.tints
        roster.push(str(folder / 'palette.py'), FILES['palette.py'].replace('(0.1, 0.2, 0.3)', '(0.8, 0.2, 0.8)'), 1)
        wait(lambda: analysis.tints is not marks, 'the roster wakes who shows what hung on the file')
        assert analysis.tints.on(10, 10) == [(10, Mark('wash', 13, 16, (0.8, 0.2, 0.8), 1.))] and app.stamp == landed
        # A change nothing here hangs on wakes nobody.
        marks, woken = analysis.tints, []

        class Watcher:
            wake = staticmethod(lambda: woken.append(1))

        watcher = Watcher()
        roster.watch(watcher, analysis.needs)
        roster.push(str(folder / 'pkg/sub/deep.py'), 'def far():  # [tint=(1.0, 1.0, 1.0)]\n    pass\n', 1)
        roster.settle()
        time.sleep(editor_model.QUIET + .1)
        assert analysis.tints is marks and woken == []
        # A source without a roster is as before: its own tints, plain marks.
        alone = editor_model.Analysis(opened(folder / 'app.py', text))
        wait(lambda: alone.tints is not None)
        assert alone.tints.on(8, 10) == [(8, Mark('tint', 0, None, (0.5, 0.1, 0.1)))]
        alone.close(), alone.source.close()
    finally:
        analysis.close()
        app.close()


def test_a_pending_save_event_pushes_the_files_text(made, monkeypatch):
    from meltygui.editor.pending_save import PendingSave
    roster, folder = made
    palette = str(folder / 'palette.py')
    texts = {palette: FILES['palette.py'].replace('(0.9, 0.5, 0.1)', '(0.0, 0.0, 1.0)')}
    asked = []
    monkeypatch.setattr(PendingSave, 'current_file_text', classmethod(lambda cls, path, canonical_file=False: asked.append(threading.current_thread()) or texts.get(path)))
    monkeypatch.setattr(roster, 'gather', lambda: None)
    roster.listen()
    try:
        before = roster.generation(palette)
        PendingSave._announce_change(types.SimpleNamespace(path=palette))
        wait(lambda: roster.generation(palette) > before, 'the announced edit reaches the table')
        assert roster.named('Ground')[0][5] == [0.0, 0.0, 1.0] and threading.current_thread() not in asked, 'the text is read on a pool thread, not where the event was announced'
        # An edit dropped again is announced too: the text is what it is then, a newer generation.
        texts[palette] = FILES['palette.py']
        PendingSave._announce_change(types.SimpleNamespace(path=palette))
        wait(lambda: roster.named('Ground')[0][5] == list(GROUND))
        # Files that are not the project's, or no Python, are left alone.
        count = len(asked)
        PendingSave._announce_change(types.SimpleNamespace(path='/elsewhere/other.py'))
        PendingSave._announce_change(types.SimpleNamespace(path=str(folder / 'notes.txt')))
        wait(lambda: not roster.feeding)
        assert len(asked) == count
    finally:
        PendingSave.unsubscribe(roster.changed)


def test_one_roster_per_project_is_kept_by_the_runtime(tmp_path, monkeypatch):
    from meltygui.core.melty import Melty
    from meltygui.editor.pending_save import PendingSave
    from meltygui.model import roster_model
    monkeypatch.setattr(Melty, 'rosters', {})
    folder = Path(os.path.realpath(tmp_path))
    (folder / 'one.py').write_text('def first():  # [tint=(0.1, 0.1, 0.1)]\n    pass\n')
    (folder / 'two.py').write_text('from one import first\nfirst()\n')
    roster = roster_model.project(folder / 'two.py')
    try:
        assert roster_model.project(folder / 'one.py') is roster and list(Melty.rosters) == [str(folder)]
        wait(lambda: roster.native.counts()[1] == 2, 'the project\'s files are gathered and read off the calling thread')
        assert roster.resolve(str(folder / 'two.py'), 'first')[:2] == (str(folder / 'one.py'), 'first')
    finally:
        PendingSave.unsubscribe(roster.changed)
        roster.close()


def test_a_module_outside_the_project_is_read_when_an_import_leads_to_it(tmp_path):
    folder, site = Path(os.path.realpath(tmp_path)) / 'project', Path(os.path.realpath(tmp_path)) / 'site'
    (site / 'lib').mkdir(parents=True)
    (site / 'lib/__init__.py').write_text('from .widgets import button\n')
    (site / 'lib/widgets.py').write_text('@render_func(tint=(0.2, 0.4, 0.6))\ndef button(label):\n    return label\n')
    folder.mkdir()
    (folder / 'app.py').write_text('from lib import button\nimport lib.widgets\nimport missing.module\nbutton("go"), lib.widgets.button, missing.module.thing\n')
    roster = Roster(folder, paths=[str(folder / 'app.py')], roots=[str(folder), str(site)])
    try:
        roster.settle()
        app = opened(folder / 'app.py')
        rows, needs = roster.tints(app)
        assert [row[:3] for row in rows] == [(0, 16, 6), (3, 0, 6), (3, 26, 6)] and rows[0][3:] == (0.2, 0.4, 0.6, 1.)
        assert roster.lookup(app, 3, 2) == (False, [(str(site / 'lib/widgets.py'), 1, 4, 6, False)])
        assert roster.native.counts()[0] == 1 and roster.native.counts()[5] == 2, 'the two library files were read, and are not the project\'s'
        assert roster.named('button') == [], 'its names are no guesses'
        assert [use[1:3] for use in roster.uses(str(site / 'lib/widgets.py'), 'button')] == [(0, 16), (3, 0), (3, 26)], 'the project\'s uses of it are known'
        assert not roster.stale(needs)
        app.close()
    finally:
        roster.close()


def test_the_writer_edits_while_another_thread_asks_the_roster(made):
    """`tints` and `lookup` let the interpreter go: the source must not be borrowed meanwhile."""
    roster, folder = made
    app = opened(folder / 'app.py', FILES['app.py'] * 60)
    roster.join(app)
    stop, failed = threading.Event(), []

    def read():
        try:
            while not stop.is_set():
                assert len(roster.tints(app)[0]) % 8 == 0
                roster.lookup(app, 8, 11)
        except Exception as error:
            failed.append(error)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for version in range(2, 200):
            app.splice(0, 0, '\n', version)
            app.ask(now=True)
            assert app.land()
    finally:
        stop.set()
        reader.join()
    assert not failed, failed
    roster.settle()
    assert roster.lookup(app, 198 + 8, 11) == (False, [(str(folder / 'pkg/shapes.py'), 4, 6, 6, False)])
    app.close()


def test_resolution_agrees_with_the_python_roster_where_its_tables_are_right():
    """On every eighth file of this checkout, without the guess (that hangs on every table at once: the
    whole comparison is in the report). Where the Python roster resolves a chain through an entry and an
    import that are real, the Rust roster resolves it to the same entry; where only Rust resolves, an
    import led there that the Python roster does not follow (a re-export, a star import's imports)."""
    import meltygui.code.symbol_roster as oracle
    root = str(ROOT)
    project = oracle.analysis_project(None, str(ROOT / 'meltygui/model/editor_model.py'))
    if project.root != root:
        pytest.skip('the checkout is not its own project here')
    paths = oracle.universe_paths(project)
    roster = Roster(root, project)
    roster.gather()
    roster.settle()
    tables = {}

    def real(path):
        """(the dotted names, the import aliases) the Rust reader finds in a file."""
        if path not in tables:
            source = opened(Path(path))
            tables[path] = {row[0] for row in source.table[0]}, {row[0] for row in source.table[1]}
            source.close()
        return tables[path]

    same = more = noise = 0
    try:
        with oracle.pass_scope():
            for path in paths[::8]:
                table, text = oracle.table_for(path), oracle.file_text(path)
                starts, seen = oracle._line_starts(text), set()
                for start, _, chain in oracle.iter_chains(text):
                    scope = table.scope_at(bisect.bisect_right(starts, start))
                    scope = scope.qualname if scope is not None else None
                    if (scope, chain) in seen:
                        continue
                    seen.add((scope, chain))
                    want = oracle.resolve(path, chain, table, allow_fallback=False, scope=table.by_qualname.get(scope), project=project)
                    want = (want.path, want.qualname) if want is not None else None
                    got = (roster.resolve(path, chain, scope, False) or (None,))[:2]
                    got = got if got[0] is not None else None
                    first = chain.split('.')[0]
                    if want == got:
                        same += want is not None
                    elif want is None:
                        assert first in real(path)[1] or first not in real(path)[0], (path, scope, chain, got)
                        more += 1
                    else:
                        wrong = want[1] not in real(want[0])[0] or (first in table.imports and first not in real(path)[1]) or (first in table.by_qualname and first not in real(path)[0])
                        assert wrong, (path, scope, chain, want, got)
                        noise += 1
    finally:
        roster.close()
    assert same > 4000 and noise < same * .05 and more < same * .05, (same, more, noise)
