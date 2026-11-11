"""Suggestion, snippet and import flows of the native editor (no window)."""
from meltygui.core.runtime.toggles import Toggles, Snippet
from meltygui.editor.rows import Numbers
from meltygui.editor import edit, marks, suggest
from meltygui.editor.editor import Editor
from meltygui.editor.syntax import tools


def session(text, caret=None):
    tools()
    ed = Editor(text)
    ed.sync()
    ed.doc.select(len(text) if caret is None else caret)
    return ed


def type_in(ed, text, **options):
    for char in text:
        ed.step = None
        edit.insert(ed.doc, char)
        ed.step = None
        ed.sync()
        ed.suggest.refresh(ed, True, **options)


def test_scope_names_rank_and_accept_replaces_the_whole_word():
    ed = session('def run(counter, other):\n    return \n', caret=len('def run(counter, other):\n    return '))
    type_in(ed, 'cou')
    assert ed.suggest.rows[0] == ('counter', 'param') or ed.suggest.rows[0][0] == 'counter'
    assert ed.suggest.accept(ed) == 'counter'
    assert ed.doc.line(1) == '    return counter' and not ed.suggest.open
    ed.doc.select(ed.doc.caret - 4)
    ed.suggest.refresh(ed, False, explicit=True)
    assert ed.suggest.open and ed.suggest.prefix == 'cou'
    ed.suggest.index = [name for name, _ in ed.suggest.rows].index('counter')
    ed.suggest.accept(ed)
    assert ed.doc.line(1) == '    return counter', 'the tail of the old word is replaced, not doubled'


def test_suggestions_stay_closed_in_comments_and_on_complete_symbols():
    ed = session('value = 1\n# val')
    ed.suggest.refresh(ed, True)
    assert not ed.suggest.open
    ed = session('value = 1\nvalue')
    ed.suggest.refresh(ed, True)
    assert not ed.suggest.open
    ed.suggest.refresh(ed, False, explicit=True)
    assert ('value', 'name') in ed.suggest.rows or any(name == 'value' for name, _ in ed.suggest.rows)


def test_a_custom_source_supplies_the_rows():
    ed = session('x = ')
    type_in(ed, 'al', source=lambda text, anchor, prefix, dot: [('alpha', 'var'), ('beta', 'var'), ('alto', 'var')])
    assert [name for name, _ in ed.suggest.rows] == ['alpha', 'alto']
    ed.suggest.step(1)
    ed.suggest.accept(ed)
    assert ed.doc.text == 'x = alto'


def test_members_of_a_loaded_imported_module():
    ed = session('import os.path as paths\nfrom os import path\nx = ')
    type_in(ed, 'paths.jo')
    assert ed.suggest.rows[0][0] == 'join'
    assert suggest.loaded_members('from os import path\npath.', 25)
    assert suggest.loaded_members('import nothing_loaded\nnothing_loaded.', 37) == []


def test_snippet_rows_insert_their_template_with_the_caret_at_the_stop(monkeypatch):
    monkeypatch.setattr(Toggles.TextEditor, 'AC_SNIPPETS', {'#[': Snippet('tint', '# [tint=($0)]')})
    ed = session('')
    type_in(ed, '#[')
    assert ed.suggest.rows == [('tint', 'snip')]
    ed.suggest.accept(ed)
    assert ed.doc.text == '# [tint=()]' and ed.doc.caret == len('# [tint=(')


def test_an_import_shortcut_row_completes_the_name_and_adds_its_import():
    ed = session('import os\n\nvalue = ')
    type_in(ed, 'path_fin')
    names = dict(ed.suggest.rows)
    assert names.get('path_finder') == 'auto_import'
    ed.suggest.index = [name for name, _ in ed.suggest.rows].index('path_finder')
    ed.suggest.accept(ed)
    lines = ed.doc.text.split('\n')
    assert 'from meltygui.core.conversion import path_finder' in lines
    assert lines[-1] == 'value = path_finder' and ed.doc.caret == len(ed.doc.text)
    from meltygui.state.core_undo import UndoManager
    UndoManager.undo()
    assert ed.doc.text == 'import os\n\nvalue = path_fin', 'the name and its import are one step'


def test_import_fix_inserts_after_the_import_block_and_keeps_the_caret():
    ed = session('import os\n\nx = np.zeros(3)\n', caret=15)
    assert suggest.fix_import(ed, 'import numpy as np')
    assert ed.doc.text == 'import os\nimport numpy as np\n\nx = np.zeros(3)\n'
    assert ed.doc.text[ed.doc.caret:ed.doc.caret + 2] == 'np'
    assert not suggest.fix_import(ed, 'import numpy as np')
    assert ed.sync() and ed.rows.count == 5


def test_marks_from_parameters():
    numbers = Numbers()
    found = marks.problems([(3, 'a'), (3, 'b'), (1, 'c')], {3: ('import x',)}, numbers)
    assert [(line, mark.shape) for line, mark in found.on(0, 9)] == [
        (0, 'under'), (0, 'number'), (0, 'error'), (2, 'under'), (2, 'number'), (2, 'error')]
    assert found.on(2)[2][1].data == ('a; b', ['import x']) and found.on(0)[2][1].data == ('c', [])
    assert [line for line, _ in marks.problems(SyntaxError('bad', ('x', 3, 1, 'x')), None, numbers).on(0, 9)] == [2, 2, 2]

    class Store:
        __live_values__ = {('line:2#total',): 3, ('line:2#mean',): 1.5, ('other',): 1, ('line:9#big',): object()}
    found = marks.values(Store, numbers)
    assert [(mark.data.name, mark.data.text) for _, mark in found.on(1)] == [('total', '3'), ('mean', '1.5')]
    assert found.on(8)[0][1].data.text is None
    assert marks.values(Store, Numbers(2)).on(0)[0][1].data.name == 'total' and marks.values(None, numbers) is None
    assert marks.breaks({4, 2}, numbers).on(0, 9) == [(1, marks.Mark('dot')), (3, marks.Mark('dot'))]
    assert [mark.shape for _, mark in marks.pause(3, numbers).on(2)] == ['band', 'arrow'] and marks.pause(None, numbers) is None
    diff = Numbers(table=[10, 11, None, 12, 40])
    assert [diff.number(line) for line in range(6)] == [10, 11, None, 12, 40, None] and diff.line(12) == 3 and diff.line(13) is None
    assert marks.breaks({12, 99, 40}, diff).on(0, 9) == [(3, marks.Mark('dot')), (4, marks.Mark('dot'))]
    assert Numbers(120).number(0) == 120 and Numbers(120).line(125) == 5 and Numbers(120).line(7) is None
    assert Numbers(120).digits(900) == 4 and diff.digits(3) == 5
    text = '@x(tint=(1, 0, 0))\ndef a():\n    pass\n'
    found = marks.tints((((0, 0, 2, (1, 0, 0)),), ((23, 24, (1, 0, 0), 1.),)), text, version=4)
    assert found.version == 4 and found.on(0, 9) == [(1, marks.Mark('tint', 0, None, (1, 0, 0))),
                                                     (1, marks.Mark('wash', 4, 5, (1, 0, 0)))]


def test_a_menu_keeps_its_selection_among_the_rows_shown():
    menu = suggest.Menu()
    menu.set([(f'row {n}', 'tag') for n in range(25)])
    assert menu.open and (menu.index, menu.first) == (0, 0) and menu.extra(3) == (None, '')
    menu.step(-1)
    assert (menu.index, menu.first) == (24, 15), 'around the end, and into view'
    menu.step(1)
    assert (menu.index, menu.first) == (0, 0)
    menu.page(1)
    menu.page(1)
    assert (menu.index, menu.first) == (20, 11)
    menu.page(5)
    assert (menu.index, menu.first) == (24, 15)
    menu.scroll(-4)
    assert (menu.index, menu.first) == (24, 11), 'the wheel moves the rows, not the selection'
    menu.point(2)
    assert menu.index == 13
    menu.scroll(-99)
    menu.point(40)
    assert (menu.index, menu.first) == (24, 0)
    menu.set(menu.rows[:5])
    assert (menu.index, menu.first) == (4, 0) and menu.picked is None
