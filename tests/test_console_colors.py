"""ANSI foreground parsing and viewport geometry for draw_text."""
from meltygui.view.text_view import _console_text, _console_color_tokens


def test_colors_reset_and_extended_foregrounds():
    text, spans = _console_text('\x1b[31mred\x1b[0m plain\x1b[94m bright'
                                '\x1b[38;5;196m cube\x1b[38;5;244m gray'
                                '\x1b[38;2;10;20;30m rgb\x1b[39m end')
    assert text == 'red plain bright cube gray rgb end'
    assert [color for _, _, color in spans] == [
        (205, 49, 49), 'default', (59, 142, 234), (255, 0, 0),
        (128, 128, 128), (10, 20, 30), 'default']


def test_background_controls_and_partial_stream():
    source = '\x1b[32mgreen\n\x1b[48;2;31;39;0mstill green'
    text, spans = _console_text(source + '\x1b[38;2;255;')
    assert text == 'green\nstill green'
    assert all(color == (13, 188, 121) for _, _, color in spans)
    assert _console_text(source + '\x1b[38;2;255;0;0mred')[1][-1][2] == (255, 0, 0)
    assert _console_text('\x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\\x1b[2K')[0] == 'link'
    assert _console_text('a\x1b')[0] == 'a'
    assert _console_text('a\x1b[38;2;999;0;0mb')[0] == 'ab'


def test_visible_window_and_clipped_tokens_keep_offsets():
    text, spans = _console_text('before\n\x1b[31mred\nnext\x1b[0m end')
    tokens = _console_color_tokens([(text[9:12], 'clipped'), (text[12:], 'default')], 9, spans)
    assert ''.join(part for part, _ in tokens) == text[9:]
    assert tokens == [('d\nn', 'clipped'), ('ext', (205, 49, 49)), (' end', 'default')]


def test_plain_log_highlighting_and_ansi_priority():
    from meltygui.view.text_view import _console_log_spans, _CONSOLE_LOG_COLORS
    source = '2026-10-08 16:32:06.999 Melty[7153:2122487] [startup] ready: 7.1 ms tint=(0.25, 1)'
    text, ansi = _console_text(source)
    spans = _console_log_spans(text, ansi)
    def color_at(fragment):
        at = text.index(fragment)
        return next(color for a, b, color in spans if a <= at < b)
    assert color_at('2026') == color_at('16:32') == _CONSOLE_LOG_COLORS['timestamp']
    assert color_at('7153') == color_at('7.1') == color_at('0.25') == _CONSOLE_LOG_COLORS['number']
    assert color_at('tint') == _CONSOLE_LOG_COLORS['field']
    assert color_at('Melty') == _CONSOLE_LOG_COLORS['text']
    assert spans[0][0] == 0 and spans[-1][1] == len(source)
    assert all(left[1] == right[0] for left, right in zip(spans, spans[1:]))
    text, ansi = _console_text('\x1b[31m' + source + '\x1b[0m 42')
    spans = _console_log_spans(text, ansi)
    assert spans[0] == (0, len(source), (205, 49, 49))
    assert spans[-1][2] == _CONSOLE_LOG_COLORS['number']
    assert _console_log_spans('', []) == []


def test_traceback_source_uses_python_lexer_and_preserves_text():
    from meltygui.view.text_view import _console_log_spans
    from meltygui.editor.text_editor import tokenize
    source = '    return int("bad") + 42\n'
    log = ('Traceback (most recent call last):\n'
           '  File "/tmp/example.py", line 12, in calculate\n' + source +
           'ValueError: invalid literal\nordinary 23\n')
    text, ansi = _console_text(log)
    spans = _console_log_spans(text, ansi)
    def keys(start, end):
        return [color for a, b, color in spans for _ in range(max(a, start), min(b, end))]
    start = log.index(source)
    assert keys(start, start + len(source)) == [key for token, key in tokenize(source) for _ in token]
    for fragment, key in [('"/tmp/example.py"', 'string'), ('12', 'number'),
                          ('calculate', 'def_name'), ('ValueError', 'builtin'), ('23', 'number')]:
        at = log.index(fragment)
        assert keys(at, at + len(fragment)) == [key] * len(fragment)
    assert spans[0][0] == 0 and spans[-1][1] == len(log)
    text, ansi = _console_text('\x1b[31m' + log + '\x1b[0m')
    assert _console_log_spans(text, ansi) == [(0, len(log), (205, 49, 49))]
