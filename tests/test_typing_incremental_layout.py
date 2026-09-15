"""Incremental edit paths must agree with a fresh layout/roster build."""
import random

from meltygui.editor.text import _fold_build
from meltygui.editor.text import _fold_update_inline
from meltygui.editor.text import _same_guide_shape
from meltygui.editor.text import _scope_guide_segments
from meltygui.editor.text import _update_line_widths
from meltygui.editor.text import _FoldLineNumbers
import meltygui.code.symbol_roster as symbol_roster


def test_inline_fold_edits_match_full_build():
    text = 'import a\nimport b\n\ndef outer():\n    x = 123\n    if x:\n        value = x\n    return x\n\ntail = 1\n'
    ranges = [(0, 1), (3, 7), (5, 6)]
    for collapsed in (set(), {(0, 1)}, {(5, 6)}, {(0, 1), (5, 6)}, {(3, 7)}):
        built = _fold_build(text, ranges, collapsed)
        for pos in range(len(text)):
            for removed, inserted in ((0, 'z'), (1, ''), (1, 'word')):
                changed = text[:pos] + inserted + text[pos + removed:]
                result = _fold_update_inline(text, changed, built)
                if result is not None:
                    assert result == _fold_build(changed, ranges, collapsed), (collapsed, pos, removed, inserted)


def test_line_width_splices_and_guide_reuse():
    text = 'class C:\n    def f(self):\n        x = 1  \n        return x\n\n'
    widths = [len(line.rstrip()) for line in text.split('\n')]
    for pos in range(len(text) + 1):
        for removed, inserted in ((0, 'a'), (1, ''), (1, '\n'), (0, '\n\n'), (0, ': # note')):
            changed = text[:pos] + inserted + text[pos + removed:]
            assert _update_line_widths(text, changed, widths) == [len(line.rstrip()) for line in changed.split('\n')]
            if _same_guide_shape(text, changed):
                assert _scope_guide_segments(text) == _scope_guide_segments(changed)


def test_lazy_gutter_matches_projection():
    mapping = [0, 1, 5, 6, 9]
    assert list(_FoldLineNumbers(mapping, [8, 9, 10, None, 12, 13])) == [8, 9, 13, None, None]
    assert list(_FoldLineNumbers(mapping, offset=100)) == [101, 102, 106, 107, 110]


def table_shape(table):
    return ([tuple(getattr(entry, field) for field in entry.__slots__) for entry in table.entries],
            table.imports, table.star_imports, table.nlines)


def test_roster_resumed_scans_match_fresh_builds():
    text = 'import os\n' + ''.join(f'\n# [tint=(0.2, 0.3, 0.4)]\ndef f{i}():\n    x = {i}\n    return x\n' for i in range(70))
    table = symbol_roster.extract_table('/tmp/roster_incremental.py', text)
    rng = random.Random(42)
    for index in range(80):
        position = rng.randrange(len(text))
        removed = rng.randrange(4)
        inserted = rng.choice(('z', '\n', '    ', 'def new(): pass\n', '', '\nfrom os import path\n'))
        changed = text[:position] + inserted + text[position + removed:]
        incremental = symbol_roster.extract_table(table.path, changed, previous=table)
        fresh = symbol_roster.extract_table(table.path, changed)
        assert table_shape(incremental) == table_shape(fresh), index
        text, table = changed, incremental


def test_region_checks_keep_line_diff_semantics():
    from meltygui.code.new_converters import _region_compile_check
    from meltygui.code.new_converters import _compile_check
    def reference(old, new):
        a, b = old.split('\n'), new.split('\n')
        pre = 0
        while pre < min(len(a), len(b)) and a[pre] == b[pre]:
            pre += 1
        if pre == len(a) == len(b):
            return 'skip', None, None
        suffix = 0
        while suffix < min(len(a) - pre, len(b) - pre) and a[-1-suffix] == b[-1-suffix]:
            suffix += 1
        start = min(pre, len(b) - 1)
        while start > 0 and (not b[start] or b[start][0] in ' \t'):
            start -= 1
        end = len(b) - suffix
        while end < len(b) and (not b[end] or b[end][0] in ' \t'):
            end += 1
        old_end = max(start, end + len(a) - len(b))
        span = start, old_end, len(b) - len(a)
        error = _compile_check('\n'.join(b[start:end]))
        if error is None:
            return 'clean', None, span
        if error.lineno:
            error.lineno += start
        status = 'error' if _compile_check('\n'.join(a[start:old_end])) is None else 'ambiguous'
        return status, error, span
    def shape(result):
        status, error, span = result
        return status, (error.msg, error.lineno, error.offset) if error else None, span
    for text in ('a = 1\n\nb = 2\n', 'def f():\n    return 1\n\nvalue = f()\n', 'a\nb', '\n\n'):
        for pos in range(len(text) + 1):
            for removed, inserted in ((0, 'x'), (1, ''), (0, '\n'), (2, '\n\nx')):
                changed = text[:pos] + inserted + text[pos + removed:]
                assert shape(_region_compile_check(text, changed, 10000)) == shape(reference(text, changed)), (text, pos, removed, inserted)


def test_cached_import_scan_defers_discovery(monkeypatch):
    import meltygui.code.code_checks as code_checks
    monkeypatch.setattr(code_checks, '_file_binds_cache', {})
    monkeypatch.setattr(code_checks, '_import_suggestion_cache', {})
    def unexpected(*args, **kwargs):
        raise AssertionError('render-thread scan attempted import discovery')
    monkeypatch.setattr(code_checks, '_module_text_binds', unexpected)
    monkeypatch.setattr(code_checks, '_suggest_import', unexpected)
    assert code_checks._scan_slice('unknown_name()', 0, set(), '/tmp/a.py', cached_only=True) is None
    monkeypatch.setattr(code_checks, '_file_binds_cache', {'/tmp/a.py': (0, 0, set(), 0)})
    assert code_checks._scan_slice('unknown_name()', 0, set(), '/tmp/a.py', cached_only=True) is None
    assert code_checks._scan_slice('len(items)', 0, {'items'}, '/tmp/a.py', cached_only=True) == {}


def test_line_offset_cache_eviction_keeps_other_buffers():
    import meltygui.editor.text as text_editor
    saved = dict(text_editor._LINE_STARTS_CACHE)
    try:
        text_editor._LINE_STARTS_CACHE.clear()
        buffers = [f'buffer{i}\nsecond\n' for i in range(17)]
        offsets = [text_editor._line_starts(text) for text in buffers[:16]]
        text_editor._line_starts(buffers[-1])
        assert len(text_editor._LINE_STARTS_CACHE) == 16
        assert text_editor._line_starts(buffers[1]) is offsets[1]
    finally:
        text_editor._LINE_STARTS_CACHE.clear()
        text_editor._LINE_STARTS_CACHE.update(saved)
