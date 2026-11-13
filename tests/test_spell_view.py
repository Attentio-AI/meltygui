"""Spell check marks through the native editor view (hidden GL window, reserved desktop)."""
import time

import meltygui.view.editor_view as view
from editor_host import Host
from meltygui.core.runtime.toggles import Toggles
from meltygui.editor import syntax
from meltygui.model import spell_model
from meltygui.model.source_model import Source
from meltygui.model.spell_model import Spelling
from meltygui.state.editor_state import EditorState

TEXT = '# a smal comment\nvalue = 1\n# anothr line\n'


def test_the_editor_underlines_the_words_the_model_found(monkeypatch):
    monkeypatch.setattr(Toggles.TextEditor, 'enable_spell_check', True)
    monkeypatch.setattr(spell_model, 'QUIET', 0.02)
    syntax.tools()
    drawn, paint = [], view.paint_squiggle
    monkeypatch.setitem(view.SHAPES, 'squiggle', (view.OVER, lambda row, mark: drawn.append((row.line, row.text[mark.low:mark.high]))
                                                 or paint(row, mark)))
    source = Source(TEXT)
    spelling = Spelling(source)
    state = EditorState()
    host = Host(view.draw_editor, input_value=source, name='editor', state=state, left=0., top=0., width=500., height=200.,
                marks={'spelling': spelling.marks})

    def rested():
        spelling.feed()
        until = time.monotonic() + 30.
        while spelling.checked != source.native.version:
            assert time.monotonic() < until
            time.sleep(0.01)
        host.frame(marks={'spelling': spelling.marks})
    try:
        assert not drawn
        rested()
        assert sorted(set(drawn)) == [(0, 'smal'), (2, 'anothr')]
        # Typing keeps the underline on its word; the next reading takes it off the corrected one.
        ed = state.editor
        host.click(ed.x(0, 8) + 1 - ed.scroll_x, ed.lh * .5)
        drawn.clear()
        host.frame(keys=[(ord('L'), 0)], text='l')
        assert source.text.startswith('# a small comment') and (0, 'smal') in drawn, 'still there until the text rests'
        drawn.clear()
        rested()
        assert sorted(set(drawn)) == [(2, 'anothr')]
        monkeypatch.setattr(Toggles.TextEditor, 'enable_spell_check', False)
        spelling.feed()
        drawn.clear()
        host.frame(marks={'spelling': spelling.marks})
        assert not drawn and ed.marks['spelling'] is None
    finally:
        spelling.close()
        host.close()
