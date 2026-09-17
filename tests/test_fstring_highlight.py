"""
f-string syntax highlighting in the draw_text tokenizer (editor/text_editor.py).

  * literal text keeps 'string'; `{` `}` `!r` `:` are 'fstring_delim'; the
    expression is ordinary code tokens; the format spec is 'string' with its
    own nested fields
  * `{{` / `}}` / `\\N{..}` are not fields; plain strings are untouched
  * half-typed f-strings never crash, never lose characters and do not leak
    past the closing quote
  * the windowed / banded tokenizers (what draw_text really calls) colour every
    character the same as the whole-text tokenizer
"""

import conftest  # noqa: F401

import pytest

import meltygui
from meltygui.editor.text_editor import COLORS
from meltygui.editor.text_editor import tokenize
from meltygui.editor.text_editor import _line_offsets
from meltygui.editor.text_editor import _line_open_full
from meltygui.editor.text_editor import _line_open_full_ref
from meltygui.editor.text_editor import _update_line_open
from meltygui.editor.text_editor import _window_tokens

D = 'fstring_delim'


def toks(src):
    out = list(tokenize(src))
    assert ''.join(t for t, _k in out) == src
    return out


def kinds_per_char(tokens):
    return [kind for tok, kind in tokens for _c in tok]


def test_simple_field():
    assert toks('f"{value}"') == [
        ('f"', 'string'), ('{', D), ('value', 'default'), ('}', D), ('"', 'string')]


def test_conversion_spec_and_escapes():
    assert toks("f'{a + b!r:>10} text {{escaped}}'") == [
        ("f'", 'string'), ('{', D), ('a', 'default'), (' ', 'default'), ('+', 'default'),
        (' ', 'default'), ('b', 'default'), ('!r', D), (':', D), ('>10', 'string'), ('}', D),
        (" text {{escaped}}'", 'string')]


def test_nested_fields_in_format_spec():
    assert toks('f"{y:{w}.{p}f}"') == [
        ('f"', 'string'), ('{', D), ('y', 'default'), (':', D),
        ('{', D), ('w', 'default'), ('}', D), ('.', 'string'),
        ('{', D), ('p', 'default'), ('}', D), ('f', 'string'), ('}', D), ('"', 'string')]


def test_expression_is_real_code():
    got = toks('''f"n={len(d['k:}'])} {x if ok else None} {1.5e3}"''')
    assert ('len', 'builtin') in got
    assert ("'k:}'", 'string') in got          # `:` and `}` inside a nested string
    assert ('if', 'keyword') in got and ('None', 'keyword_const') in got
    assert ('1.5e3', 'number') in got
    assert got.count(('{', D)) == 3 and got.count(('}', D)) == 3
    assert (':', D) not in got


def test_operators_that_look_like_field_syntax():
    got = toks('f"{a != b} {x=} {-5} {d[1:2]} {(lambda: 0)()}"')
    assert (':', D) not in got and not any(t.startswith('!') and k == D for t, k in got)
    assert ('-5', 'number') in got             # unary sign merges after the `{`
    assert ('lambda', 'keyword') in got


@pytest.mark.parametrize('prefix', ['f', 'F', 'rf', 'fr', 'Rf', 'fR', 'RF'])
@pytest.mark.parametrize('quote', ['"', "'", '"""', "'''"])
def test_prefixes_and_quote_styles(prefix, quote):
    got = toks(f'{prefix}{quote}a {{v}} b{quote} + 1')
    assert got[:5] == [(prefix + quote + 'a ', 'string'), ('{', D), ('v', 'default'),
                       ('}', D), (' b' + quote, 'string')]
    assert got[-1] == ('1', 'number')


def test_raw_fstring_backslash_is_not_an_escape():
    assert ('{', D) in toks(r'rf"\d{n}"')
    assert toks(r'f"\N{EM DASH} {a}"')[0] == (r'f"\N{EM DASH} ', 'string')
    assert ('{', D) in toks(r'rf"\N{a}"')


def test_nested_fstring_in_expression():
    got = toks('''f"{f'{inner}' + x}"''')
    assert got.count(('{', D)) == 2
    assert ('inner', 'default') in got and ("f'", 'string') in got


@pytest.mark.parametrize('src', [
    '"{plain}"', "'{a!r:>3}'", 'r"{x}"', 'b"{x}"', 'rb"{x}"', 'u"{x}"',
    '"""doc {x}"""', 'f"no fields"', 'f"{{only}} escapes"', 'f""', "f''''''"])
def test_strings_without_fields_stay_one_token(src):
    got = toks(src)
    assert len(got) == 1 and got[0][1] in ('string', 'string_doc')


def test_stray_close_brace_is_literal():
    assert toks('f"a } b"') == [('f"a } b"', 'string')]


HALF_TYPED = ['f"', 'f"{', 'f"{x', 'f"{x!', 'f"{x!r', 'f"{x:', 'f"{x:>', 'f"{x:{', 'f"{x:{w',
              'f"{(', "f\"{d['", 'f"{x}', 'f"{{', 'f"}', 'f"\\', 'f"{\\', 'f"""{x', 'f"""{x:{y}"',
              'f"{x!r junk}"', 'f"{:{:{:{:{:{:{:{:"']


@pytest.mark.parametrize('src', HALF_TYPED)
def test_half_typed_does_not_crash_or_lose_text(src):
    got = toks(src)
    assert all(kind in COLORS for _t, kind in got)


def test_every_prefix_of_a_line_tokenizes():
    line = '''print(f"{name!r:>{width}} = {vals[i]['k'] * 2:.3f} {{x}}", end="")  # done'''
    for cut in range(len(line) + 1):
        got = toks(line[:cut])
        assert all(kind in COLORS for _t, kind in got)


def test_unclosed_field_does_not_swallow_code_after_the_closing_quote():
    got = toks('s = f"{x" + foo(1)  # note')
    assert got[4:8] == [('f"', 'string'), ('{', D), ('x', 'default'), ('"', 'string')]
    assert ('foo', 'default') in got and ('1', 'number') in got
    assert got[-1] == ('# note', 'comment')


def test_unterminated_single_quoted_field_stops_at_the_line_end():
    got = toks('a = f"{x\nb = 1\n')
    assert got[4:7] == [('f"', 'string'), ('{', D), ('x', 'default')]
    # the rest is the (unterminated) string, not a run-away expression
    assert got[7:] == [('\nb = 1\n', 'string')]


MULTILINE = '''\
x = 1
msg = f"""head {x}
  row {x + 1:>{w}} and {{esc}}
  {fn(a, "s")!r} tail"""
t = """plain {x}
doc"""
y = rf'{x}\\d' + f"{x:
z = 2
'''


def test_line_open_records_fstring_state_and_matches_reference():
    offs, line_open = _line_open_full(MULTILINE)
    assert (offs, line_open) == _line_open_full_ref(MULTILINE)
    assert line_open[:4] == [None, None, ('"""', 'fstring'), ('"""', 'fstring')]
    assert line_open[5] == ('"""', 'string_doc')
    assert line_open[7] == ('"', 'fstring')


def test_incremental_line_open_matches_full():
    before = MULTILINE.replace('f"""head', '"""head')
    offs, line_open = _line_open_full(before)
    assert _update_line_open(before, offs, line_open, MULTILINE) == _line_open_full(MULTILINE)


def test_window_tokens_colour_like_the_whole_text():
    want = kinds_per_char(tokenize(MULTILINE))
    offs, line_open = _line_open_full(MULTILINE)
    nlines = len(offs)
    for v0 in range(nlines):
        for v1 in range(v0, nlines):
            _wl, start, got = _window_tokens(MULTILINE, offs, line_open, v0, v1, lookback=0)
            kinds = kinds_per_char(got)
            assert kinds == want[start:start + len(kinds)], (v0, v1)
    # the resumed lines really carry fields (not one flat string)
    _wl, _start, got = _window_tokens(MULTILINE, offs, line_open, 2, 2, lookback=0)
    assert ('{', D) in got and ('w', 'default') in got and ('1', 'number') in got


def test_long_line_band_cut_inside_a_field():
    text = 'v = f"' + 'ab ' * 200 + '{alpha + beta(1):>{width}} ' + 'cd ' * 200 + '" + 1\n'
    want = kinds_per_char(tokenize(text))
    offs, line_open = _line_open_full(text)
    field = text.index('{alpha')
    for c0 in range(field - 3, field + 30):
        _wl, _start, got = _window_tokens(text, offs, line_open, 0, 0,
                                          band=(c0, c0 + 40, 100))
        assert ''.join(t for t, _k in got) == text
        for i, kind in enumerate(kinds_per_char(got)):
            assert kind == 'clipped' or kind == want[i], (c0, i)


def test_package_under_test_is_this_checkout():
    from pathlib import Path
    assert Path(meltygui.__file__).resolve().parents[1] == Path(__file__).resolve().parents[1]
    assert _line_offsets('a\nb') == [0, 2]
