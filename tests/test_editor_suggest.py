"""Suggestion, snippet and import flows of the native editor (no window)."""
from meltygui.core.runtime.toggles import Toggles, Snippet
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
        edit.insert(ed.doc, char, merge=True)
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
    ed.doc.undo(); ed.doc.undo()
    assert ed.doc.text == 'import os\n\nvalue = path_fin'


def test_import_fix_inserts_after_the_import_block_and_keeps_the_caret():
    ed = session('import os\n\nx = np.zeros(3)\n', caret=15)
    assert suggest.fix_import(ed, 'import numpy as np')
    assert ed.doc.text == 'import os\nimport numpy as np\n\nx = np.zeros(3)\n'
    assert ed.doc.text[ed.doc.caret:ed.doc.caret + 2] == 'np'
    assert not suggest.fix_import(ed, 'import numpy as np')
    assert ed.sync() and ed.rows.count == 5


def test_marks_from_parameters():
    assert marks.errors([(3, 'a'), (3, 'b'), (1, 'c')]) == {2: 'a; b', 0: 'c'}
    assert marks.fixes({4: ('import x',)}) == {3: ['import x']}

    class Store:
        __live_values__ = {('line:2#total',): 3, ('line:2#mean',): 1.5, ('other',): 1, ('line:9#big',): object()}
    found = marks.live(Store)
    assert [(value.name, value.text) for value in found[1]] == [('total', '3'), ('mean', '1.5')]
    assert found[8][0].text is None
    assert marks.live(Store, offset=1)[0][0].name == 'total'
    assert marks.moved({1: 'a', 3: 'b', 5: 'c'}, 2, 2) == {1: 'a', 5: 'b', 7: 'c'}
    assert marks.moved({1: 'a', 3: 'b', 5: 'c'}, 2, -2) == {1: 'a', 3: 'c'}
    ed = session('@x(tint=(1, 0, 0))\ndef a():\n    pass\n')
    blocks, washes = marks.tints((((0, 0, 2, (1, 0, 0)),), ((23, 24, (1, 0, 0), 1.),)), ed.doc)
    assert blocks == [(0, 2, 0, (1, 0, 0))] and washes == {1: [(4, 1, (1, 0, 0))]}
