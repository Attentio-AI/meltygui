"""The Rust `Source` (native/source): its text store, the facts it reads, its
thread, and the `Rows` a session keeps the fold ranges in.

`ast` is the oracle for what Python defines (definitions, their extents, the
names each scope binds) on every Python file here. The fold scanner of the
legacy editor (`_scope_fold_ranges`) is the oracle for fold ranges wherever
no statement continues over lines in a way it misread: it took every line
for a statement, the Rust reader follows open brackets (`bent` counts the
lines where that makes a difference).
"""
import ast
import random
import threading
import time
import zlib
from pathlib import Path

import pytest

from meltygui.editor.rows import Rows
from meltygui.editor.syntax import tools, LEGACY
from meltygui.model.source_model import Source

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted(path for folder in ('meltygui', 'tests', 'examples', 'tools') for path in (ROOT / folder).rglob('*.py'))


def stamp(path):
    """A number that is the file's own: of its path under the checkout, whatever else is in the tree."""
    return zlib.crc32(path.relative_to(ROOT).as_posix().encode())


def sample(share):
    """About one file in `share`, picked by its path: a file added to the tree changes no other pick."""
    return [path for path in FILES if stamp(path) % share == 0]
SAMPLE = (ROOT / 'meltygui/editor/editor.py').read_text()


def nest(ranges):
    """(heads, spans, parents) of `(head, last)` ranges: sorted outermost
    first, each with the index of the range around it (-1: none)."""
    ranges = sorted(ranges, key=lambda item: (item[0], -item[1]))
    parents, open_ranges = [], []
    for at, (head, last) in enumerate(ranges):
        while open_ranges and ranges[open_ranges[-1]][1] < head:
            open_ranges.pop()
        parents.append(open_ranges[-1] if open_ranges else -1)
        open_ranges.append(at)
    return [head for head, _ in ranges], [last - head for head, last in ranges], parents, ranges


def oracle(text):
    """The fold ranges of `text` as `Source.read` returns them, by the Python scanner."""
    ranges, defaults, keys = tools()._scope_fold_ranges(text)
    heads, spans, parents, ranges = nest([item for item in ranges if item[1] > item[0]])
    lines = text.split('\n')
    names, scopes = [], []
    for head, last in ranges:
        name = keys[(head, last)]
        names.append(name)
        line = lines[head]
        scopes.append((len(line) - len(line.lstrip(' ')), name[0] == 'scope') if name[0] in ('scope', 'block') else None)
    return heads, spans, parents, names, scopes, sorted((keys[item] for item in defaults if item in keys), key=repr)


def folds(source):
    heads, spans, parents, names, scopes, defaults = source.native.read()[1]
    return heads, spans, parents, names, scopes, sorted(defaults, key=repr)


def bent(source):
    """Lines where the reader's continued statements part from the legacy scanner."""
    return source.native.read()[4]


def bindings(tree):
    """{line of the def or class, -1 for the module: {name}}: what each scope binds, by `ast`."""
    found = {}

    def scope(node, line):
        names = found.setdefault(line, set())
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            names.update(arg.arg for arg in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg] if arg)

        def visit(child):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(child.name)
                return scope(child, child.lineno - 1)
            if isinstance(child, ast.Lambda):
                return
            if isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                # A comprehension is a scope of its own; only a `:=` inside it binds in ours.
                return names.update(inner.target.id for inner in ast.walk(child) if isinstance(inner, ast.NamedExpr))
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                names.update((alias.asname or alias.name.split('.')[0]) for alias in child.names if alias.name != '*')
            elif isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)
            elif isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
                names.add(child.name)
            elif isinstance(child, ast.MatchMapping) and child.rest:
                names.add(child.rest)
            for inner in ast.iter_child_nodes(child):
                visit(inner)

        outside = [getattr(node, 'args', None), getattr(node, 'returns', None), *getattr(node, 'decorator_list', []),
                   *getattr(node, 'bases', []), *getattr(node, 'keywords', [])]
        for child in ast.iter_child_nodes(node):
            if not any(child is other for other in outside):
                visit(child)

    scope(tree, -1)
    return found


def hits(text, term):
    """Search hits as the Python editor found them."""
    found, needle = [], term.lower()
    for line, row in enumerate(text.lower().split('\n')):
        at = row.find(needle)
        while at >= 0:
            found.append((line, at, at + len(needle)))
            at = row.find(needle, at + len(needle))
    return found


def test_the_store_matches_a_python_string_under_random_edits():
    random.seed(4)
    pieces = ['', 'x', 'é', '日本', '\n', '\n\n', 'def f():\n', '"""', "'", '#', ' ' * 4, 'line\n' * 70, '\tß\n']
    for _ in range(40):
        text = ''.join(random.choice(pieces) for _ in range(random.randint(0, 60)))
        source = Source(text)
        for version in range(1, 60):
            low = random.randint(0, len(text))
            high = random.randint(low, min(len(text), low + random.choice([0, 0, 1, 5, 400])))
            new = ''.join(random.choice(pieces) for _ in range(random.randint(0, 3)))
            text = text[:low] + new + text[high:]
            source.splice(low, high, new, version)
            native = source.native
            assert (native.size, native.lines, native.version) == (len(text), text.count('\n') + 1, version)
        assert source.text == text


def test_folds_agree_with_the_python_scanner_wherever_it_read_statements_right():
    assert len(FILES) > 300
    same = 0
    for path in FILES:
        text = path.read_text()
        source = Source(text)
        if not bent(source):
            assert folds(source) == oracle(text), path
            same += 1
    assert same > len(FILES) * .9, 'the two part on few files'


def test_bindings_agree_with_ast_on_every_file_here():
    pairs = wrong = 0
    for path in FILES:
        text = path.read_text()
        try:
            want = bindings(ast.parse(text))
        except SyntaxError:
            continue
        source = Source(text)
        defs = source.read()[2]
        source.ask(now=True)
        assert source.land()
        got, lines = {}, text.split('\n')
        for line, column, name, kind, scope in source.bindings(0, 10 ** 9):
            assert lines[line][column:column + len(name)] == name
            if kind not in ('global', 'nonlocal', 'comp', 'lambda', 'del'):
                got.setdefault(defs[scope][2] if scope >= 0 else -1, set()).add(name)
        for key in set(got) | set(want):
            pairs += len(got.get(key, set()) | want.get(key, set()))
            wrong += len(got.get(key, set()) ^ want.get(key, set()))
    assert pairs > 50000 and wrong <= pairs * .001, f'{wrong} of {pairs} scope names differ from ast'


def test_definitions_end_where_ast_ends_them_or_on_their_trailing_comments():
    total = longer = 0
    for path in FILES:
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.split('\n')
        ends = {node.lineno - 1: node.end_lineno - 1 for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for is_class, name, line, last, indent, parent in Source(text).read()[2]:
            total += 1
            if ends[line] != last:
                # The body's last lines are comments: they fold with it.
                assert last > ends[line] and all(not row.strip() or row.lstrip().startswith('#') for row in lines[ends[line] + 1:last + 1]), (path, line)
                longer += 1
    assert total > 5000 and longer < total * .02


def test_a_statement_continued_over_lines_neither_ends_a_scope_nor_opens_one():
    text = ('def spread(\n    first,\n    second=(1, 2),\n    *rest,\n):\n    total = first\n    return total\n\n'
            'value = compute(\ndef_like, 1)\nclass After:\n    pass\n')
    source = Source(text)
    assert bent(source) == 1 and source.read()[2] == [(False, 'spread', 0, 6, 0, -1), (True, 'After', 10, 11, 0, -1)]
    source.ask(now=True)
    source.land()
    assert [(name, kind, scope) for _, _, name, kind, scope in source.bindings(0, 99)] == [
        ('spread', 'def', -1), ('first', 'param', 0), ('second', 'param', 0), ('rest', 'param', 0), ('total', 'assign', 0),
        ('value', 'assign', -1), ('After', 'class', -1)]
    # A bracket left open while typing ends at the next statement that cannot be inside one.
    broken = Source('x = call(a,\ny = 2\ndef f():\n    z = 3\n')
    broken.ask(now=True)
    broken.land()
    assert broken.read()[2] == [(False, 'f', 2, 3, 0, -1)] and ('z', 'assign', 0) in [found[2:] for found in broken.bindings(0, 9)]


@pytest.mark.parametrize('line, names', [
    ('a = b = 1', 'a b'), ('a, (b, *c) = x', 'a b c'), ('a.b = c[d] = e', ''), ('x: int = 3', 'x'), ('y: str', 'y'), ('n += 1', 'n'),
    ('if (m := f()): pass', 'm'), ('for i, (j, k) in rows: pass', 'i j k'), ('with a() as b, c as (d, e): pass', 'b d e'),
    ('import a.b, c as d', 'a d'), ('from a import b as c, d', 'c d'), ('from a import (b,\n    c as d)', 'b d'),
    ('try: pass\nexcept (E, F) as err: pass', 'err'), ('f = lambda p, q=1: p', 'f'), ('a = 1; b = 2', 'a b'),
    ('def f(a, b=g(c), *d, e: T = None, **h) -> R: i = 1', 'f a b d e h i'), ('class K(Base, m=M): attr = 1', 'K attr'),
    ('print(end=x)', ''), ('s = f"{t} = {u}"', 's'), ('d = {k: v for k, v in rows}', 'd k v'), ('if a == b: c = 1', 'c'),
    ('(a,\n b) = pair', 'a b'), ('(a,\n b)', ''), ('for a, (b,\n  c) in rows: pass', 'a b c'), ('with open(p,\n  mode) as handle: pass', 'handle'),
    ('match command:\n    case [x, y] | (x, *y): pass', 'x y x y'), ('match c:\n    case Point(x=px, y=0) as p if px > limit: q = 1', 'px p q'),
    ('match c:\n    case {"k": v, **rest}: pass', 'v rest'), ('match c:\n    case os.sep | limit.value | str() | -1 | None | _: pass', ''),
    ('match (a, b):\n    case (\n        one,\n        Point(x=two),\n    ) as both:\n        pass', 'one two both'),
    ('case = 1\nmatch = case(2)\ncase(\n  a,\n  b)\nmatch[0] = case.x\n', 'case match'),
])
def test_what_a_statement_binds(line, names):
    source = Source(line + '\n')
    source.ask(now=True)
    source.land()
    assert ' '.join(name for _, _, name, kind, _ in source.bindings(0, 9) if kind != 'lambda') == names


@pytest.mark.parametrize('text', [
    '', '\n', 'x', 'def f(): pass', 'def f():\n    pass', 'def f():\n    pass\n',
    'class A:\n\tdef m(self):\n\t\treturn 1\n',                              # tabs
    'async def go():\n    async with x:\n        pass\n    async for y in z:\n        pass\n',
    'def f():\n    s = """\ndef fake():\n    pass\n"""\n    return s\n',      # a def inside a string
    's = "a \\" \'\'\' b"\ndef f():\n    pass\n',                            # a triple quote inside a string
    "x = 1  # ''' not a string\ndef f():\n    pass\n",                       # ... and after a comment
    's = """a""" + """b\nc\nd"""\nx = 1\n',                                  # two strings on a line
    '# [tint=(1, 0, 0)]\nx = 1\n',                                           # a lone metadata comment
    '# one\n# two\n    # three\n    # four\n\n# five\n',                     # comment runs by indent
    'import a\nfrom b import (\n    c,\n    d,\n)\nimport e \\\n    , f\nx = 1\nimport late\n',
    'def f():\n    pass\ndef f():\n    pass\n',                              # twins
    'if a:\n    pass\nelif b:\n    pass\nelse:\n    pass\n',
    'if x: # note: with a colon\n    pass\nfor  y in z :   \n    pass\nformat(x):\n    pass\n',
    'class Ünï:\n    def möthod(self):\n        return "é"\n',
    'def f(\n        a,\n        b):\n    return a\n',
    'try:\n    pass\nexcept (A,\n        B):\n    pass\nfinally:\n    pass\n',
    'match x:\n    case 1:\n        pass\n    case _:\n        pass\n',
    'def f():\n    x = 1\n# back at column 0\n    y = 2\n',                     # a comment ends the scope, as the scanner has it
    'x = """\n',                                                              # an unclosed string
    'def a():\n    def b():\n        def c():\n            pass\n\n\n',
    'a\r\nb\r\n', 'def f():\x0c\n    pass\n',
])
def test_folds_agree_on_the_awkward_cases(text):
    assert not bent(Source(text)) and folds(Source(text)) == oracle(text)


def test_facts_after_edits_are_the_facts_of_the_text():
    """Chunks keep what was read from them: an edit must leave nothing stale."""
    random.seed(11)
    text = SAMPLE
    source = Source(text)
    source.native.read()
    pieces = ['x', ' ', '\n', '"""', "'''", '"', '#', ':', 'def new():\n', '    ', 'class K:\n', '\n\n', 'if a:\n', 'import os\n']
    for version in range(1, 400):
        low = random.randint(0, len(text))
        high = random.randint(low, min(len(text), low + random.choice([0, 0, 0, 1, 3, 30, 900])))
        new = random.choice(pieces) if random.random() < .8 else ''
        text = text[:low] + new + text[high:]
        source.splice(low, high, new, version)
        if version % 3 == 0:
            assert source.native.read() == Source(text, version=version).native.read(), version
    assert source.text == text and (bent(source) or folds(source) == oracle(text))


def test_definitions_agree_with_ast_on_every_file_here():
    for path in FILES:
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        want = sorted((node.name, node.lineno - 1, isinstance(node, ast.ClassDef)) for node in ast.walk(tree)
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        defs = Source(text).native.read()[2]
        assert sorted((name, line, is_class) for is_class, name, line, *_ in defs) == want, path
        for is_class, name, line, last, indent, parent in defs:
            assert last >= line and (parent < 0 or defs[parent][2] < line <= defs[parent][3])


def uses(tree, lines):
    """(references, calls) by `ast`: every `Name` with the dotted chain it heads as (line, column, chain), and
    the calls of such chains with (positional arguments, keyword names, has `*`, has `**`). The names in
    the replacement fields of f-strings are among them."""
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    refs, calls = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name):
            continue
        chain, top = node.id, node
        while isinstance(parent.get(top), ast.Attribute) and parent[top].value is top:
            top = parent[top]
            chain += '.' + top.attr
        at = (node.lineno - 1, len(lines[node.lineno - 1].encode()[:node.col_offset].decode()), chain)
        refs.add(at)
        call = parent.get(top)
        if isinstance(call, ast.Call) and call.func is top:
            calls.add(at + (sum(not isinstance(arg, ast.Starred) for arg in call.args), tuple(key.arg for key in call.keywords if key.arg),
                            any(isinstance(arg, ast.Starred) for arg in call.args), any(key.arg is None for key in call.keywords)))
    return refs, calls


def test_references_and_calls_agree_with_ast_on_every_file_here():
    names = wrong = made = miscalled = 0
    for path in FILES:
        text = path.read_text()
        try:
            refs, calls = uses(ast.parse(text), text.split('\n'))
        except SyntaxError:
            continue
        source = Source(text)
        source.ask(now=True)
        assert source.land()
        got = {(line, column, ''.join(chain.split())) for line, column, chain, _ in source.references(0, 10 ** 9)}
        names += len(got | refs)
        wrong += len(got ^ refs)
        found = source.calls(0, 10 ** 9)
        # A call whose closing bracket is in another block of lines has no argument shape (-1): not compared.
        shaped = {(line, column, ''.join(chain.split()), positional, tuple(keys), star, more)
                  for line, column, chain, positional, keys, star, more in found if positional >= 0}
        unshaped = {call[:2] for call in found if call[3] < 0}
        calls = {call for call in calls if call[:2] not in unshaped}
        made += len(shaped | calls)
        miscalled += len(shaped ^ calls)
        assert len(unshaped) <= len(found) * .05 + 3, path
    assert names > 200000 and wrong <= names * .0005, f'{wrong} of {names} references differ from ast'
    assert made > 50000 and miscalled <= made * .0005, f'{miscalled} of {made} calls differ from ast'


def test_what_counts_as_a_reference_and_a_call():
    text = ('import os.path as paths\nfrom a import b\n\n@wraps(fn)\ndef run(first, second=default, *rest, key: Kind = None):\n'
            '    total = first.size + helper(second, *rest, key=key, **options)\n    global shared\n'
            '    return [item.name for item, _ in pairs(total)], max(x for x, y in rows), (lambda a, b=c: a)(1)\n'
            'class Kind(Base, metaclass=Meta):\n    pass\n')
    source = Source(text)
    source.ask(now=True)
    source.land()
    refs = [chain for _, _, chain, _ in source.references(0, 99)]
    assert 'os' not in refs and 'paths' not in refs and 'b' not in refs and 'run' not in refs and 'shared' not in refs, 'names only bound'
    assert 'metaclass' not in refs and refs.count('key') == 1 and 'first.size' in refs and 'Base' in refs and 'c' in refs
    assert [(chain, positional, keys, star, more) for _, _, chain, positional, keys, star, more in source.calls(0, 99)] == [
        ('wraps', 1, [], False, False), ('helper', 1, ['key'], True, True), ('pairs', 1, [], False, False), ('max', 1, [], False, False)]
    assert source.references(5, 5)[0] == (5, 4, 'total', True) and source.calls(3, 3) == [(3, 1, 'wraps', 1, [], False, False)]
    # The replacement fields of an f-string are code: over lines too, and never the escapes.
    quoted = Source('s = f"{first!r:>{width}} {{no}} \\N{DASH} {call(a, b=1)}"\nt = rf\'\'\'x\n{second:{pad}} {open\n\'\'\'\nlast = 1\n')
    quoted.ask(now=True)
    quoted.land()
    assert [chain for _, _, chain, _ in quoted.references(0, 9)] == ['s', 'first', 'width', 'call', 'a', 't', 'second', 'pad', 'last']
    assert quoted.calls(0, 0) == [(0, 42, 'call', 1, ['b'], False, False)] and quoted.statements(0, 9) == [(0, 0), (1, 3), (4, 4)]


def test_statement_extents_and_parameter_defaults_agree_with_ast_on_every_file_here():
    compound = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.AsyncFor, ast.While, ast.With,
                ast.AsyncWith, ast.Try, ast.Match, getattr(ast, 'TryStar', ast.Try))
    simple = missing = params = wrong = 0
    for path in FILES:
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.split('\n')
        chars = lambda line, column: len(lines[line].encode()[:column].decode())
        source = Source(text)
        source.ask(now=True)
        assert source.land()
        found = dict(source.statements(0, 10 ** 9))
        for node in ast.walk(tree):
            if isinstance(node, ast.stmt) and not isinstance(node, compound):
                simple += 1
                # A statement after another on its line (`a; b`, `if x: y`) is within the line's statement.
                first, last = node.lineno - 1, node.end_lineno - 1
                missing += not (found.get(first, -1) >= last if first in found else first == last)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                plain = args.posonlyargs + args.args
                want = list(zip(plain, [None] * (len(plain) - len(args.defaults)) + args.defaults))
                want += [(args.vararg, None)] * bool(args.vararg) + list(zip(args.kwonlyargs, args.kw_defaults)) + [(args.kwarg, None)] * bool(args.kwarg)
                want = [(arg.arg, value and (value.lineno - 1, chars(value.lineno - 1, value.col_offset), value.end_lineno - 1,
                                             chars(value.end_lineno - 1, value.end_col_offset))) for arg, value in want]
                got = [(name, span) for name, _, _, span in source.parameters(node.lineno - 1)]
                params += len(want)
                wrong += sum(a != b for a, b in zip(got, want)) + abs(len(got) - len(want))
    assert simple > 50000 and missing <= simple * .0005, f'{missing} of {simple} statements are not where ast has them'
    assert params > 10000 and wrong <= params * .001, f'{wrong} of {params} parameters differ from ast'


def test_statements_and_parameters_of_a_small_text():
    text = ('@decorated\ndef run(first, second=(1,\n        2), *rest, key: Kind = lambda a, b: a, **more):\n'
            '    value = call(\n\n        first)  # note\n    text = """a\nb"""\n    if value: return\n')
    source = Source(text)
    source.ask(now=True)
    source.land()
    assert source.statements(0, 99) == [(0, 0), (1, 2), (3, 5), (6, 7), (8, 8)] and source.statements(4, 7) == [(6, 7)]
    assert source.parameters(1) == [('first', 1, 8, None), ('second', 1, 15, (1, 22, 2, 10)), ('rest', 2, 13, None),
                                    ('key', 2, 19, (2, 31, 2, 45)), ('more', 2, 49, None)]
    assert source.parameters(3) == [], 'not a def'


def undefined(text, path=None, live=()):
    """{(file line, name)} of the names the source finds undefined."""
    source = Source(text, path)
    source.lint = True
    source.live(live)
    source.ask(now=True)
    assert source.land()
    return {(line + 1, name) for line, _, name, known, noted in source.undefined if not known and not noted}, source


def by_oracle(text, path=None):
    """{(file line, name)} of the undefined names `code_checks.check_source` reports."""
    import re
    from meltygui.code import code_checks
    found = (re.match(r"name '(\w+)' is not defined", message) and (line, re.match(r"name '(\w+)'", message).group(1))
             for line, message in code_checks.check_source(text, path, max_reports=10 ** 9))
    return {item for item in found if item}


def test_undefined_names_agree_with_the_lint_oracle_on_every_file_here():
    from meltygui.code import code_checks
    reported = 0
    for path in FILES:
        text = path.read_text()
        module = code_checks._module_for(str(path))
        live = set(vars(module)) if module is not None else set()
        got, source = undefined(text, str(path), live)
        # A name the live module has is still reported when an import would bring it (the oracle's rule).
        got |= {(line + 1, name) for line, _, name, known, noted in source.undefined
                if known and not noted and code_checks._suggest_import_for_path(name, str(path))}
        assert got == by_oracle(text, str(path)), path
        reported += len(got)
    assert reported > 5


def test_undefined_names_agree_with_the_oracle_when_a_binding_is_taken_out():
    """Every name the source reports, the oracle reports (no false alarm). The oracle may report a few more:
    the source binds a lambda's parameters and a comprehension's targets in the function they are written in."""
    import re
    texts = found = silent = 0
    for path in sample(9):
        rows = path.read_text().split('\n')
        picks = [at for at, row in enumerate(rows) if re.match(r'(import |from \w[\w.]* import \w|[A-Za-z_]\w* = |def \w+\(|    [a-z_]\w* = )', row)
                 and not row.rstrip().endswith(('(', '\\', ','))]
        # (The lines taken out of a file are its own too: seeded by its path, not by the files before it.)
        for at in random.Random(stamp(path)).sample(picks, min(4, len(picks))):
            row = rows[at]
            changed = re.sub(r'def (\w+)', r'def \1_renamed', row) if row.startswith('def ') else row[:len(row) - len(row.lstrip())] + 'pass'
            text = '\n'.join(rows[:at] + [changed] + rows[at + 1:])
            try:
                ast.parse(text)
            except SyntaxError:
                continue
            got, want = undefined(text)[0], by_oracle(text)
            assert got <= want, (path, at, sorted(got - want)[:3])
            texts += 1
            found += len(got)
            silent += len(want - got)
    assert texts > 200 and found > 300 and silent <= found * .02, f'{silent} of {found + silent} names the oracle reports are missed'


def test_import_suggestions_are_the_oracles_for_the_names_really_undefined():
    """`collect_import_suggestions` scans tokens against a flat set of bound names: it also suggests imports for
    annotated fields, lambda parameters, keyword names and unpacked targets. The source suggests for undefined
    names only: every one of its suggestions is among the oracle's, and none for a name the lint oracle misses."""
    from meltygui.code import code_checks
    suggest = code_checks._suggest_import_for_path
    ours = theirs = 0
    for path in FILES:
        text = path.read_text()
        want = {(line, statement) for line, statements in code_checks.collect_import_suggestions(text, str(path), full=True).items()
                for statement in statements}
        source = undefined(text, str(path))[1]
        got = {(line + 1, statement) for line, _, name, _, _ in source.undefined for statement in suggest(name, str(path)) or ()}
        assert got <= want, (path, sorted(got - want)[:3])
        assert {(line, statement) for line, name in by_oracle(text, str(path)) for statement in suggest(name, str(path)) or ()} <= got, path
        ours += len(got)
        theirs += len(want)
    assert ours > 10 and theirs >= ours


MATCH = '''import os
def run(command, Point, limit):
    match command:
        case [x, y]:
            print(x, y)
        case (a, b):
            print(a, b)
        case Point(x=px, y=0) as p if px > limit:
            print(px, p)
        case {"k": v, **rest}:
            print(v, rest)
        case [first, *others]: print(first, others)
        case str() | int():
            pass
        case os.sep | limit.value:
            pass
        case (
            one,
            Point(x=two),
        ):
            print(one, two)
        case _:
            print(nowhere, _)
    match (command, limit):
        case _: pass
    match = 1
    case = match + 1
    print(match(case))
'''


def test_a_case_binds_the_names_its_pattern_captures():
    """`match` and `case` are soft keywords: a header's first word is no name used, a capture is bound in the
    scope around, a class, a dotted value and the guard are names used, `_` in a pattern is neither."""
    got, source = undefined(MATCH)
    assert got == by_oracle(MATCH) == {(23, 'nowhere'), (23, '_')}
    want = bindings(ast.parse(MATCH))
    assert {name for _, _, name, kind, scope in source.bindings(0, 99) if scope == 0 and kind != 'param'} == want[1] - {'command', 'Point', 'limit'}
    used = lambda line: {name for _, _, name, store in source.references(line, line) if not store}
    assert used(2) == {'command'} and used(3) == set() and used(7) == {'Point', 'px', 'limit'} and used(11) == {'print', 'first', 'others'}
    assert used(12) == {'str', 'int'} and used(14) == {'os.sep', 'limit.value'} and used(18) == {'Point'} and used(21) == set()
    assert used(23) == {'command', 'limit'}


def test_what_counts_as_an_undefined_name():
    text = ('import os\nfrom a import b as c\nLIMIT = 1\n\nclass Shape:\n    sides = 4\n    def area(self, scale: Unknown = LIMIT) -> Result:\n'
            '        global total\n        total = sides + scale + os.sep + c + later + missing.attr\n        later = [n for n in rows if (m := n)]\n'
            '        return f"{nowhere!r} {total}" + str(lambda z: z + q)\n\ndef one(x): return x + y\n'
            'try:\n    probe\nexcept NameError:\n    other\nprint(total, undefined_here)\ndel gone\nprint(gone)\n')
    got, source = undefined(text)
    assert got == {(9, 'sides'), (9, 'missing'), (10, 'rows'), (11, 'nowhere'), (11, 'q'), (13, 'y'), (17, 'other'), (18, 'undefined_here')}
    assert source.problems[0] == (9, "name 'sides' is not defined"), 'a class body is out of sight of its methods'
    assert undefined(text, live={'rows', 'q', 'other'})[0] == {(9, 'sides'), (9, 'missing'), (11, 'nowhere'), (13, 'y'), (18, 'undefined_here')}
    assert undefined('from a import *\nprint(anything)\n')[0] == set(), 'a star import may bind any name'
    assert undefined('x: Missing = 1\ndef f(a: Missing, *b: Missing) -> Missing:\n    return a, b, x\n')[0] == set(), 'annotations are not names used'


def miscalls(text):
    """{(line, message)} of the calls the source finds not to fit what they call."""
    source = Source(text)
    source.lint = True
    source.ask(now=True)
    assert source.land()
    return {(line, message) for line, _, message in source.miscalls}


def miscalls_by_oracle(text):
    """{(line, message)} of `code_checks._check_call_static` (the check of calls to definitions in the
    buffer that `check_source` runs on every call), uncapped; None when the text does not parse."""
    from meltygui.code import code_checks
    try:
        collector = code_checks._Collector()
        collector.run(code_checks._parse_for_analysis(text))
    except (SyntaxError, ValueError, RecursionError):
        return None
    found = ((call.lineno - 1, code_checks._check_call_static(call, scope)) for call, scope, _ in collector.calls)
    return {item for item in found if item[1]}


ARGUMENTS = ('{}, zz_bogus=1', '1, 2, 3, 4, 5, 6, 7, 8, 9, {}', '', '*(1, 2), {}', '"text", {}', '*[1, 2, 3, 4, 5, 6, 7], **more', 'True, -2.5, {}', '1')
HEADERS = ('zz_new, ', 'zz_new: int = 3, /, ', '*zz_all, ')
DECORATORS = ('@staticmethod', '@classmethod', '@window(title="x")', '@other')


def miscalled(text):
    """`text` with half of its calls of names it defines given other arguments, and a fifth of its
    functions another header or a decorator; None when it does not parse."""
    import re
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    made = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    raw = text.encode()
    starts = [0]
    for row in raw.split(b'\n'):
        starts.append(starts[-1] + len(row) + 1)
    spans = []
    for node in ast.walk(tree):
        func = getattr(node, 'func', None)
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else None
        if not isinstance(node, ast.Call) or name not in made or random.random() > .5:
            continue
        low, high = starts[func.end_lineno - 1] + func.end_col_offset, starts[node.end_lineno - 1] + node.end_col_offset - 1
        if raw[low:low + 1] == b'(' and raw[high:high + 1] == b')':
            spans.append((low, high))
    kept = []
    for low, high in sorted(spans):
        if not kept or low >= kept[-1][1]:
            kept.append((low, high))
    for low, high in reversed(kept):
        raw = raw[:low + 1] + random.choice(ARGUMENTS).format(raw[low + 1:high].decode()).encode() + raw[high:]
    rows = []
    for row in raw.decode().split('\n'):
        found = re.match(r'(\s*)(async )?def \w+\(', row)
        if found and random.random() < .2:
            pick = random.randrange(7)
            if pick < 3:
                row = row[:found.end()] + HEADERS[pick] + row[found.end():]
            else:
                rows.append(found.group(1) + DECORATORS[pick - 3])
        rows.append(row)
    return '\n'.join(rows)


def test_call_signatures_agree_with_the_oracle_on_every_file_here():
    reported = 0
    for path in FILES:
        text = path.read_text()
        want = miscalls_by_oracle(text)
        if want is not None:
            assert miscalls(text) == want, path
            reported += len(want)
    assert reported >= 1


def test_call_signatures_agree_with_the_oracle_when_calls_and_headers_are_changed():
    """Every misfit the source reports, the oracle reports (no false alarm). The oracle may report a few
    more: a call in a lambda written in a class body, and one in an f-string that nests its own quote."""
    random.seed(7)
    texts = found = silent = 0
    for path in FILES[::2]:
        text = miscalled(path.read_text())
        want = miscalls_by_oracle(text) if text is not None else None
        if want is None:
            continue
        got = miscalls(text)
        assert got <= want, (path, sorted(got - want)[:3])
        texts += 1
        found += len(got)
        silent += len(want - got)
    assert texts > 200 and found > 300 and silent * 100 <= found, (texts, found, silent)


def test_what_counts_as_a_misfitting_call():
    text = ('def f(a, b=2, *, c): pass\n'                     # 0
            'def g(*rest, **more): pass\n'
            'def typed(count: int, size: float = 1., name: str = "", on: bool = True): pass\n'
            'class Plain: pass\n'
            'class Based(dict): pass\n'
            'class Made:\n'                                    # 5
            '    def __init__(self, a, /, b): pass\n'
            '    @staticmethod\n'
            '    def still(a): pass\n'
            '    @classmethod\n'
            '    def build(cls, a): pass\n'                    # 10
            '    @property\n'
            '    def size(self): return 1\n'
            '    @window(title="x")\n'
            '    def shown(self, value): pass\n'
            '    def method(self):\n'                          # 15
            '        self.still()\n'
            '        self.build(1, 2)\n'
            '        self.size(1, 2, 3)\n'
            '        self.shown()\n'
            '        self.method(1)\n'                         # 20
            '        self.unknown(1, 2, 3)\n'
            '        lambda: self.method(1)\n'
            '        def inner(): self.method(1)\n'
            'f()\n'
            'f(1, 2, 3, c=4)\n'                                # 25
            'f(1, a=2, c=3)\n'
            'f(1, d=2)\n'
            'f(1, c=3)\n'
            'f(*args)\n'
            'f(*(1, 2, 3), c=1)\n'                             # 30
            'f(**options)\n'
            'g(1, 2, x=3)\n'
            'typed(1.5)\n'
            'typed(2.0, 1, "a", on=1)\n'
            'typed(-3, name=b"x")\n'                           # 35
            'typed(True)\n'
            'Plain(1)\n'
            'Based(1)\n'
            'Made(1)\n'
            'Made(1, a=2)\n'                                   # 40
            'def twice(a): pass\n'
            'def twice(a, b): pass\n'
            'twice()\n'
            'f = print\n')
    assert miscalls(text) == miscalls_by_oracle(text)
    assert miscalls(text) == {
        (16, "still() missing required argument: 'a'"),
        (17, 'build() takes 1 positional argument but 2 were given'),
        (19, "shown() missing required argument: 'value'"),
        (20, 'method() takes 0 positional arguments but 1 were given'),
        (33, "typed() expected int for 'count', got float"),
        (34, "typed() expected bool for 'on', got int"),
        (36, "typed() expected int for 'count', got bool"),
        (37, 'Plain() takes no arguments'),
        (39, "Made() missing required argument: 'b'"),
        (40, "Made() got an unexpected keyword argument 'a'"),
    }, 'f is bound twice (the last line), so none of its calls is judged'
    text = text.replace('f = print\n', '')
    assert miscalls(text) == miscalls_by_oracle(text)
    source = Source('def f(a: int): pass\nf()\nmissing\nf(1.5)\n')
    source.lint = True
    source.ask(now=True)
    assert source.land()
    assert source.problems == [(2, "f() missing required argument: 'a'"), (3, "name 'missing' is not defined"), (4, "f() expected int for 'a', got float")]
    assert {found for found in miscalls(text) if 24 <= found[0] <= 31} == {
        (24, "f() missing required arguments: 'a', 'c'"),
        (25, 'f() takes 2 positional arguments but 3 were given'),
        (26, "f() got multiple values for argument 'a'"),
        (27, "f() got an unexpected keyword argument 'd'"),
        (30, 'f() takes 2 positional arguments but 3 were given'),
    }


def live_miscalls(text, path):
    """{(line, message)} of the misfitting calls once the source was told the signatures it asks for."""
    from meltygui.code import code_checks
    from meltygui.model import editor_model
    source = Source(text, path)
    source.lint = True
    source.ask(now=True)
    assert source.land()
    module = code_checks._module_for(path)
    source.signatures({chain: editor_model.signature(chain, module) for chain in source.callees})
    source.ask(now=True)
    assert source.land() and not source.callees
    return {(line, message) for line, _, message in source.miscalls}, source


def live_miscalls_by_oracle(text, path):
    """{(line, message)} of the call checks `check_source` runs on a whole file: `_check_call_static`, then
    `_check_call_live` outside a `try` that catches TypeError; None when the text does not parse."""
    from meltygui.code import code_checks
    try:
        collector = code_checks._Collector()
        collector.run(code_checks._parse_for_analysis(text))
    except (SyntaxError, ValueError, RecursionError):
        return None
    live = code_checks._LiveCtx(code_checks._module_for(path), collector.declared_attrs)
    found = set()
    for call, scope, guarded in collector.calls:
        message = code_checks._check_call_static(call, scope)
        if message is None and not guarded:
            message = code_checks._check_call_live(live, call, scope)
        if message:
            found.add((call.lineno - 1, message))
    return found


def test_calls_into_the_running_process_agree_with_the_oracle_on_every_file_and_on_changed_calls():
    """Signatures of what is called in the running process are pushed in as plain rows. No false alarm; the
    oracle may report a call more in an f-string that nests its own quote."""
    random.seed(7)
    texts = found = silent = chains = 0
    for at, path in enumerate(FILES):
        text = path.read_text()
        want = live_miscalls_by_oracle(text, str(path))
        if want is None:
            continue
        got, source = live_miscalls(text, str(path))
        assert got == want, path
        chains += len(source.native.callees())
        if at % 2:
            continue
        # Every third call given other arguments.
        try:
            tree = ast.parse(text)
        except RecursionError:
            continue
        raw = text.encode()
        starts = [0]
        for row in raw.split(b'\n'):
            starts.append(starts[-1] + len(row) + 1)
        spans, kept = [], []
        for node in ast.walk(tree):
            func = getattr(node, 'func', None)
            if isinstance(node, ast.Call) and isinstance(func, (ast.Name, ast.Attribute)) and random.random() < .3:
                low, high = starts[func.end_lineno - 1] + func.end_col_offset, starts[node.end_lineno - 1] + node.end_col_offset - 1
                if raw[low:low + 1] == b'(' and raw[high:high + 1] == b')':
                    spans.append((low, high))
        for low, high in sorted(spans):
            if not kept or low >= kept[-1][1]:
                kept.append((low, high))
        for low, high in reversed(kept):
            raw = raw[:low + 1] + random.choice(ARGUMENTS).format(raw[low + 1:high].decode()).encode() + raw[high:]
        want = live_miscalls_by_oracle(raw.decode(), str(path))
        if want is None:
            continue
        got = live_miscalls(raw.decode(), str(path))[0]
        assert got <= want, (path, sorted(got - want)[:3])
        texts += 1
        found += len(got)
        silent += len(want - got)
    assert texts > 100 and found > 200 and silent * 100 <= found, (texts, found, silent)


def test_the_worker_reports_what_check_source_did_without_running_its_call_checks():
    """`editor_model.attributes` (the attribute a live module lacks, extensions) and the source's misfitting
    calls are `check_source`'s findings but the undefined names (those are compared above)."""
    from meltygui.code import code_checks
    from meltygui.model import editor_model
    reported = 0
    lacking = 'import os\nimport os.path\nos.nothing_here\nos.path.nothing(1)\nos.getcwd(1)\nlen()\ntry:\n    os.nowhere\nexcept AttributeError:\n    pass\n'
    texts = [(path, path.read_text()) for path in FILES[::3] + [ROOT / 'meltygui/code/libcst_conversion.py']]
    for path, text in texts + [(Path(code_checks.__file__), lacking)]:
        want = {found for found in code_checks.check_source(text, str(path), max_reports=10 ** 9) if not found[1].startswith("name '")}
        try:
            compile(text, str(path), 'exec')
        except (SyntaxError, ValueError):
            continue
        got = set(editor_model.attributes(text, str(path))) | {(line + 1, message) for line, message in live_miscalls(text, str(path))[0]}
        assert got == want, path
        reported += len(got)
    assert got == {(3, "module 'os' has no attribute 'nothing_here'"), (4, "module 'posixpath' has no attribute 'nothing'"),
                   (5, 'getcwd() takes 0 positional arguments but 1 were given'), (6, "len() missing required argument: 'obj'")}
    assert reported >= 5


def test_what_a_call_into_the_running_process_is():
    from meltygui.model import editor_model
    assert editor_model.signature('builtins.len', None) == [('obj', 0, True, '')]
    assert editor_model.signature('builtins.nothing_there', None) is None and editor_model.signature('os.path.join', None) is None
    import os
    rows = editor_model.signature('os.path.join', type('module', (), {'os': os}))
    assert rows[0][:3] == ('a', 1, True) and rows[-1][1] == 3
    text = ('import os\nfrom os.path import join, join as other\nimport json as os\n'       # 0..2
            'len(1, 2)\nlen()\nisinstance(1)\nos.getcwd(1)\njoin()\n'                       # 3..7
            'def f(len):\n    len(1, 2)\n'                                                  # 8..9
            'try:\n    len(1, 2)\nexcept TypeError:\n    len(1, 2)\n'                       # 10..13
            'join = 1\njoin()\nother()\nmissing(1, 2)\n')                                   # 14..17
    source = Source(text)
    source.lint = True
    source.ask(now=True)
    assert source.land() and source.miscalls == []
    assert source.callees == ['builtins.isinstance', 'builtins.len', 'builtins.missing', 'os.getcwd', 'other'], \
        'join is bound by more than imports; os by imports only'
    module = type('module', (), {'os': os, 'other': os.path.join})
    source.signatures({chain: editor_model.signature(chain, module) for chain in source.callees})
    source.ask(now=True)
    assert source.land() and source.callees == []
    assert [(line, message) for line, _, message in source.miscalls] == [
        (3, 'len() takes 1 positional argument but 2 were given'),
        (4, "len() missing required argument: 'obj'"),
        (5, "isinstance() missing required argument: 'class_or_tuple'"),
        (6, 'getcwd() takes 0 positional arguments but 1 were given'),
        (13, 'len() takes 1 positional argument but 2 were given'),
        (16, "other() missing required argument: 'a'"),
    ]


INSERTS = ('x', '_', ' ', '(', ')', ',', '=', '"""', '\n', '\n    ', '#', ':', '.', '@window\n', 'self.', '*', '[', ']', "'")
LINES_IN = ('    total = compute(1, 2)\n', 'def added(a, b=1):\n    return a\n', 'import os\n', 'value = 1\n', 'class Added:\n    def __init__(self, a): pass\n',
            '        added(1, 2, 3)\n', 'from os.path import join\n', '    try:\n        len(1, 2)\n    except TypeError:\n        pass\n', 'del value\n',
            '    match value:\n        case [first, second]: print(first)\n', 'Added()\n', 'print(nowhere_at_all)\n', 'from somewhere import *\n')


def test_kept_lint_findings_are_those_of_a_fresh_reading_under_random_edits():
    """The lint keeps each chunk's findings while what they depend on holds (`Facts::lint`). After every
    edit, what the source reports with them is what it reports once they are dropped and every chunk is
    read again. (Not compared with another source of the same text: its chunks are cut elsewhere, and a
    bracketed target list left open across a chunk boundary binds by where the boundary falls.)"""
    from meltygui.model import editor_model
    random.seed(11)
    kept = read = 0
    for path in ('meltygui/editor/editor.py', 'meltygui/model/editor_model.py', 'tests/test_source.py'):
        text = miscalled((ROOT / path).read_text()) or (ROOT / path).read_text()
        source = Source(text)
        source.lint = True
        told, live = {}, set()
        for step in range(250):
            pick = random.random()
            at = random.randrange(len(text) + 1)
            start = text.rfind('\n', 0, at) + 1
            if pick < .35:
                low, high, new = at, at, random.choice(INSERTS)
            elif pick < .5:
                low, high, new = at, min(len(text), at + random.randrange(1, 4)), ''
            elif pick < .7:
                low, high, new = start, start, random.choice(LINES_IN)
            elif pick < .8:
                end = text.find('\n', at)
                low, high, new = start, len(text) if end < 0 else end + 1, ''
            elif pick < .9:
                header = text.find('def ', at)
                paren = text.find('(', header) if header >= 0 else -1
                low, high, new = (paren + 1, paren + 1, random.choice(HEADERS)) if paren >= 0 else (at, at, '')
            elif pick < .95:
                # The running process tells its signatures, or forgets them.
                low, high, new = at, at, ''
                told = {chain: editor_model.signature(chain, None) for chain in source.callees} if random.random() < .7 else {}
                source.signatures(told)
            else:
                low, high, new = at, at, ''
                live = set(random.sample([name for _, _, name, _, _ in source.undefined] or ['x'], 1)) if random.random() < .7 else set()
                source.live(live)
            text = text[:low] + new + text[high:]
            source.splice(low, high, new, step + 1)
            source.ask(now=True)
            assert source.land() and source.text == text
            got, chunks = (source.undefined, source.miscalls, source.callees), len(source.native.blocks(0, 10 ** 9))
            kept += chunks - source.native.relinted()
            read += chunks
            # The same text in the same chunks, every chunk read again.
            source.native.forget()
            source.ask(now=True)
            assert source.land() and source.native.relinted() == chunks
            for fact, had, has in zip(('undefined', 'miscalls', 'callees'), got, (source.undefined, source.miscalls, source.callees)):
                assert had == has, (path, step, fact, [item for item in had if item not in has][:3], [item for item in has if item not in had][:3], repr(new))
    assert kept * 4 > read, f'{kept} of {read} chunk findings were kept'


BREAKS = ('(', ')', '[', ']', '{', '}', '"""', "'''", '"', ':', ' ', 'def ', '=', ',', '\n', '    ')


def by_compile(text):
    """(line, message) of the syntax error CPython finds in `text`, None when it compiles."""
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            compile(text, '<text>', 'exec', dont_inherit=True)
    except SyntaxError as error:
        return error.lineno, error.msg
    return None


def test_syntax_flaws_and_statement_by_statement_compiling_agree_with_compiling_the_file():
    """The reader's flaws (brackets, an open triple-quoted string) are never a false alarm and carry CPython's
    message. Compiling each top-level statement on its own (`Source.native.tops`) finds the error compiling the
    whole file finds; it may name the line before (a block missing where the statement ends) or another
    line the string was detected open on."""
    random.seed(3)
    texts = invalid = flawed = same = held = 0
    for at, path in enumerate(FILES):
        text = path.read_text()
        for broken in range(3 if at % 3 == 0 else 1):
            if broken:
                cut = random.randrange(len(text) + 1)
                body = text[:cut] + random.choice(BREAKS) + text[cut + random.randrange(0, 3) * (random.random() < .5):]
            else:
                body = text
            try:
                want = by_compile(body)
            except (ValueError, RecursionError):
                continue
            source = Source(body)
            source.ask(now=True)
            assert source.land()
            flaws = [(line + 1, message) for line, message in source.flaws]
            assert want is not None or not flaws, (path, flaws[:2])
            if at % 3:
                continue
            found = [by_compile('\n' * first + source.native.span(first, last)) for first, last, _ in source.native.tops()]
            found = [error for error in found if error]
            assert (want is None) == (not found), (path, want, found[:2])
            texts += 1
            invalid += want is not None
            flawed += bool(flaws)
            same += want in flaws
            held += want in found
    assert texts > 500 and invalid > 200 and flawed > 100 and same * 10 > flawed * 6, (texts, invalid, flawed, same)
    assert (invalid - held) * 50 <= invalid, f'{invalid - held} of {invalid} errors are not found statement by statement'


def test_what_the_reader_is_sure_cannot_be_python():
    def flaws(text):
        source = Source(text)
        source.ask(now=True)
        assert source.land()
        assert not source.flaws or by_compile(text), 'never a false alarm'
        return source.flaws

    assert flaws('x = (1,\n     2)\ny = [a for a in (1, 2)]\nz = {"k": f"{x[0]:>{y[0]}}"}\n') == []
    assert flaws('x = foo(1,\n        [2\ndef f():\n    pass\n') == [(1, "'[' was never closed")] and by_compile('x = foo(1,\n        [2\ndef f():\n    pass\n')[1] == "'[' was never closed"
    assert flaws('x = 1)\n') == [(0, "unmatched ')'")] and by_compile('x = 1)\n') == (1, "unmatched ')'")
    assert flaws('x = (1,\n     2]\n') == [(1, "closing parenthesis ']' does not match opening parenthesis '(' on line 1")]
    assert by_compile('x = (1,\n     2]\n') == (2, "closing parenthesis ']' does not match opening parenthesis '(' on line 1")
    assert flaws('x = [1}\n') == [(0, "closing parenthesis '}' does not match opening parenthesis '['")]
    assert flaws('a = 1\ns = """open\nmore\n') == [(1, 'unterminated triple-quoted string literal (detected at line 3)')]
    assert by_compile('a = 1\ns = """open\nmore\n') == (2, 'unterminated triple-quoted string literal (detected at line 3)')
    assert flaws('value = (\n') == [(0, "'(' was never closed")], 'where the text ends'
    source = Source('value = (\nmissing\n')
    source.lint = True
    source.ask(now=True)
    assert source.land() and source.problems == [(1, "'(' was never closed"), (2, "name 'missing' is not defined")]
    # Top-level statements: a decorator goes with its definition, a clause with its statement.
    text = '# first\nimport os\n@decorated\n\ndef f():\n    pass\nif os:\n    pass\nelse:\n    pass\ntry:\n    pass\nexcept Exception:\n    pass\nx = (1,\n2)\n'
    source = Source(text)
    source.ask(now=True)
    assert source.land() and [(first, last) for first, last, _ in source.native.tops()] == [(0, 0), (1, 1), (2, 5), (6, 9), (10, 13), (14, 15)]
    assert source.native.span(2, 5) == '@decorated\n\ndef f():\n    pass\n'


def table(text):
    """The source's symbol table: entry rows with the dotted name of the entry they are in, imports, star imports."""
    source = Source(text)
    source.ask(now=True)
    assert source.land()
    entries, imports, stars = source.native.table()
    rows = [(dotted, name, kind, line, end, indent, column, entries[parent][0] if parent >= 0 else None, shown)
            for dotted, name, kind, line, end, indent, column, parent, shown, _ in entries]
    source.tinted = {(name, kind, line): tuple(tint) for _, name, kind, line, _, _, _, _, _, tint in entries if tint}
    return rows, {alias: (module, name) for alias, module, name in imports}, stars, source


def table_by_oracle(path, text):
    """The same rows from `symbol_roster.extract_table` (lines from 0)."""
    from meltygui.code import symbol_roster
    found = symbol_roster.extract_table(str(path), text, with_tints=False)
    rows = [(entry.qualname, entry.name, entry.kind, entry.line - 1, entry.end - 1, entry.indent, entry.col, entry.parent, entry.sig)
            for entry in found.entries]
    return rows, dict(found.imports), list(found.star_imports)


def test_the_symbol_table_agrees_with_the_roster_wherever_that_reads_statements_right():
    """`extract_table` reads line by line, blind to strings and brackets. Every entry of the source is one of
    its entries; where they differ the oracle read a line inside a string or inside brackets as a statement:
    it has entries for those lines, and ends a definition there, at the `):` of its own header too (so the
    end and the dotted name of what follows differ, and the function's locals become entries). Its imports
    hold words of comments and strings after `import`."""
    entries = same = ends = dotted = alone = strings = inside = locals_ = other = imports = prose = 0
    for path in FILES:
        text = path.read_text()
        got, got_imports, got_stars, source = table(text)
        want, want_imports, want_stars = table_by_oracle(path, text)
        lines = text.count('\n') + 1
        states = list(source.native.states(0, lines))
        continued = {line for first, last in source.statements(0, lines) for line in range(first + 1, last + 1)}
        place = lambda row: (row[1], row[2], row[3], row[5], row[6], row[8])
        by_place = {place(row): row for row in want}
        for row in got:
            entries += 1
            twin = by_place.pop(place(row), None)
            same += twin == row
            alone += twin is None
            ends += twin is not None and twin[4] != row[4]
            dotted += twin is not None and twin[0] != row[0]
        functions = [(row[3], row[4]) for row in got if row[2] == 'def']
        for row in by_place.values():
            line = row[3]
            if states[line] not in (0, 255):
                strings += 1
            elif line in continued:
                inside += 1
            elif any(first < line <= last for first, last in functions):
                locals_ += 1
            else:
                other += 1
        assert got_stars == want_stars, path
        bound = {name for _, _, name, kind, _ in source.bindings(0, lines) if kind == 'import'}
        for alias, target in want_imports.items():
            imports += 1
            if got_imports.get(alias) != target:
                prose += 1
                assert alias not in got_imports and alias not in bound, (path, alias, target, got_imports.get(alias))
        assert set(got_imports) <= set(want_imports), path
    assert entries > 15000 and same * 100 >= entries * 99 and alone * 1000 <= entries, (entries, same, alone)
    assert other * 1000 <= entries, f'{other} oracle entries are on lines the source reads as statements of the module or a class'
    print(f'{entries} entries, {same} the same, {ends} end elsewhere, {dotted} with another dotted name, {alone} not in the oracle; '
          f'oracle only: {strings} in strings, {inside} inside brackets, {locals_} locals of a function it ended early, {other} other; '
          f'imports {imports}, {prose} from comments or strings')


def test_the_tints_of_the_symbol_table_are_the_ones_the_editor_reads():
    """The tint an entry's source states: a `# [tint=(..)]` comment, a decorator's `tint=(..)`, a `tint = (..)`
    in the body: as `text_editor._scan_def_tint_lines` (the resolver the roster calls) reads it for every
    entry. `extract_table` itself asks that resolver only when a line at or above the entry mentions a
    tint, so it misses a class whose only tint is in its body: counted, not asserted."""
    from meltygui.code import symbol_roster
    from meltygui.editor import text_editor
    tinted = entries = missed = deep = 0
    for path in FILES:
        text = path.read_text()
        if 'tint' not in text:
            continue
        lines = text.split('\n')
        source = table(text)[3]
        by_table = {(entry.name, entry.kind, entry.line - 1): tuple(entry.tint) for entry in symbol_roster.extract_table(str(path), text).entries if entry.tint}
        for _, name, kind, line, *_ in source.native.table()[0]:
            found = text_editor._scan_def_tint_lines(lines, line + 1, None if kind == 'var' else name)
            want = tuple(found[0][:3]) if found else None
            got = source.tinted.get((name, kind, line))
            # (A class's `tint` further down its body, or annotated: read as the editor showed it before, beyond the resolver.)
            deep += got is not None and want is None and kind == 'class'
            assert got == want or (want is None and kind == 'class'), (path, name, kind, line)
            entries += 1
            tinted += want is not None
            missed += want is not None and (name, kind, line) not in by_table
    assert entries > 5000 and tinted > 150 and missed <= tinted // 10 and deep <= tinted // 20, (entries, tinted, missed, deep)
    print(f'{tinted} tinted of {entries} entries; {missed} of them the roster table misses; {deep} classes tinted further down their body')


def test_what_the_symbol_table_holds():
    text = ('import os, a.b\nimport c.d as e\nfrom .f import g as h, i\nfrom j import (\n    k,\n    l as m,\n)\nfrom n import *\n'       # 0..7
            'LIMIT: int = 1\nLIMIT = 2\nx, y = 1, 2\n'                                                                                # 8..10
            'class Shape(Base):\n    sides = 4\n    def area(self,\n             scale=1):\n        local = 1\n'                         # 11..15
            '        def inner(): pass\n        return scale\n\n    # trailing\n'                                                     # 16..19
            '    class Inner:\n        deep = 1\n\nasync def go():\n    class InFunction:\n        hidden = 1\n'                         # 20..25
            'call(\n    keyword=1,\n)\n')                                                                                             # 26..28
    rows, imports, stars, _ = table(text)
    assert imports == {'os': ('os', None), 'a': ('a', None), 'e': ('c.d', None), 'h': ('.f', 'g'), 'i': ('.f', 'i'), 'k': ('j', 'k'), 'm': ('j', 'l')}
    assert stars == ['n']
    assert [(row[0], row[2], row[3], row[4], row[5], row[6], row[7]) for row in rows] == [
        ('LIMIT', 'var', 8, 8, 0, 0, None),
        ('Shape', 'class', 11, 22, 0, 6, None),
        ('Shape.sides', 'var', 12, 12, 4, 4, 'Shape'),
        ('Shape.area', 'def', 13, 19, 4, 8, 'Shape'),
        ('Shape.area.inner', 'def', 16, 16, 8, 12, 'Shape.area'),
        ('Shape.Inner', 'class', 20, 22, 4, 10, 'Shape'),
        ('Shape.Inner.deep', 'var', 21, 21, 8, 8, 'Shape.Inner'),
        ('go', 'def', 23, 25, 0, 10, None),
        ('go.InFunction', 'class', 24, 25, 4, 10, 'go'),
    ], 'no entry for a local, an unpacked target, a keyword argument or what a class in a function assigns'
    assert rows[3][8] == '    def area(self,' and table_by_oracle('x.py', text)[1] == imports
    assert [row for row in table_by_oracle('x.py', text)[0] if row[1] != 'keyword'] == rows, 'the oracle reads the keyword argument as a variable'


TINTED = '''import os

@window(title="x", tint=(0.1, 0.2, 0.3))
@render_func(tint=(0.4, 0.5, 0.6))
def shown(value):
    return value


# [tint=(0.9, 0.1, 0.1), show_tint=True]
class Shape:
    sides = 4  # [tint=(0.2, 0.9, 0.2)]

    def area(self):
        scale = 2.0  # [tint=(0.1, 0.1, 0.9)]
        other = scale * Shape.sides
        return shown(other) + scale


class Plain:
    def late(self):
        pass
    tint: tuple = (0.5, 0.5, 0.0)


def user(shown):
    scale = Shape()
    return shown(scale), Plain, os.sep
'''


def test_tints_and_lookup_among_the_symbols_of_the_buffer():
    source = table(TINTED)[3]
    assert source.tinted == {('shown', 'def', 4): (0.4, 0.5, 0.6), ('Shape', 'class', 9): (0.9, 0.1, 0.1),
                             ('sides', 'var', 10): (0.2, 0.9, 0.2), ('Plain', 'class', 18): (0.5, 0.5, 0.0)}, 'the decorator next to the definition states its tint'
    lines = TINTED.split('\n')
    rows = [(line, lines[line][column:column + chars] if chars else '', (red, green, blue)) for line, column, chars, red, green, blue in source.native.tints()]
    assert rows == [
        (4, '', (0.4, 0.5, 0.6)), (9, '', (0.9, 0.1, 0.1)),
        (10, 'sides', (0.2, 0.9, 0.2)),
        (13, 'scale', (0.1, 0.1, 0.9)),
        (14, 'scale', (0.1, 0.1, 0.9)), (14, 'Shape', (0.9, 0.1, 0.1)), (14, 'sides', (0.2, 0.9, 0.2)),
        (15, 'shown', (0.4, 0.5, 0.6)), (15, 'scale', (0.1, 0.1, 0.9)),
        (18, '', (0.5, 0.5, 0.0)),
        (25, 'Shape', (0.9, 0.1, 0.1)),
        (26, 'Plain', (0.5, 0.5, 0.0)),
    ], 'a parameter or a local of the same name hides the symbol (user: shown, scale); a function variable tints its uses'
    place = lambda line, word, nth=0: (line, [at for at in range(len(lines[line])) if lines[line].startswith(word, at)][nth])
    at = lambda line, word, nth=0: source.lookup(*place(line, word, nth))
    assert at(15, 'shown') == (False, [(4, 4, 5)]) and at(14, 'Shape') == (False, [(9, 6, 5)]) and at(14, 'sides') == (False, [(10, 4, 5)])
    assert at(9, 'Shape') == (True, [(14, 24, 5), (25, 12, 5)]) and at(4, 'shown') == (True, [(15, 15, 5)])
    assert at(10, 'sides') == (True, [(14, 30, 5)]) and at(26, 'shown') is None and at(26, 'os') is None and at(0, 'import') is None
    assert source.table[0][1][:8] == ('Shape', 'Shape', 'class', 9, 17, 0, 6, -1)
    from meltygui.editor import marks
    found = marks.tints(source.native.tints(), version=7)
    assert found.version == 7 and found.on(13, 14)[0] == (13, marks.Mark('wash', 8, 13, (0.1, 0.1, 0.9))) and found.on(9, 9) == [(9, marks.Mark('tint', 0, None, (0.9, 0.1, 0.1)))]


def test_the_writer_edits_while_another_thread_reads_whole_file_facts():
    """`tops`, `tints`, `table` and `lookup` let the interpreter go while they run: the source must not be
    borrowed meanwhile, or the writer's next edit raises."""
    source = Source(TINTED * 400)
    source.ask(now=True)
    assert source.land()
    stop, failed = threading.Event(), []

    def read():
        try:
            while not stop.is_set():
                source.native.tops(), source.native.tints(), source.native.table(), source.native.lookup(9, 6)
        except Exception as error:
            failed.append(error)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for version in range(2, 300):
            source.splice(5, 5, 'x', version)
            source.ask(now=True)
            assert source.land()
    finally:
        stop.set()
        reader.join()
    assert not failed, failed


def test_the_statement_table_is_the_source_site_index_on_every_file_here():
    """Every statement and compound header with its place, kind and key path: what
    `melty_scan.SourceSiteIndex` (the breakpoint sites) holds, and the lines each site is found on."""
    from meltygui.code import melty_scan
    files = sites = 0
    for path in FILES:
        text = path.read_text()
        metadata = {}
        try:
            index = melty_scan.SourceSiteIndex(text, melty_scan.scan(text, site_metadata=metadata)[0], metadata)
        except (SyntaxError, ValueError, RecursionError):
            continue
        source = Source(text)
        source.ask(now=True)
        assert source.land()
        want = {tuple(key): (site.start_line - 1, site.start_column, site.end_line - 1, site.end_column, site.kind) for key, site in index.sites.items()}
        got = {tuple(key): (line, column, last, end, kind) for line, column, last, end, kind, key in source.sites}
        assert got == want, path
        # A site is found on the lines it covers that have code (`at_line`).
        lines = text.split('\n')
        covered = {}
        for line, column, last, end, kind, key in source.sites:
            for row in range(line, last + (end != 0)):
                covered.setdefault(row, set()).add(tuple(key))
        for row, keys in covered.items():
            found = {tuple(site.path) for site in index.at_line(row + 1)}
            assert found == keys or (not found and (not lines[row].strip() or lines[row].lstrip().startswith('#'))), (path, row)
        files += 1
        sites += len(got)
    assert files > 700 and sites > 150000, (files, sites)


def test_what_a_site_is_keyed_by():
    text = ('import os\nx = 1\nx = 2\nx += 1\nobj.attr = make()\nprint(x); del x\n'                                  # 0..5
            '@deco(1)\n@other\nclass K:\n    def m(self):\n        if a:\n            return a\n'                       # 6..11
            '        elif b: pass\n        else:\n            raise E\n'                                                # 12..14
            '        for i in (rows or ()):\n            continue\n        try:\n            a.b.c(1)\n'                # 15..18
            '        except (A, B) as e:\n            pass\n        while a: a -= 1\n'                                  # 19..21
            'if x:\n    pass\n')                                                                                        # 22..23
    source = Source(text)
    source.ask(now=True)
    assert source.land()
    assert [(line, column, kind, tuple(key)) for line, column, _, _, kind, key in source.sites] == [
        (0, 0, 'Import', ('##import',)), (1, 0, 'Assign', ('x',)), (2, 0, 'Assign', ('x#1',)), (3, 0, 'Expr', ('##statement',)),
        (4, 0, 'Assign', ('make()',)), (5, 0, 'Expr', ('print()',)), (5, 10, 'Opaque', ('##del',)),
        (6, 1, 'decorator', ('K', 'decorators', 'deco')), (7, 1, 'decorator', ('K', 'decorators', 'other')), (8, 0, 'ClassDef', ('K',)),
        (9, 4, 'FunctionDef', ('K', 'm')), (10, 8, 'If', ('K', 'm', 'locals', 'if##0')), (11, 12, 'Opaque', ('K', 'm', 'locals', 'if##0', '##return')),
        (12, 8, 'If', ('K', 'm', 'locals', 'elif##0')), (12, 16, 'Opaque', ('K', 'm', 'locals', 'elif##0', '##pass')),
        (13, 8, 'else', ('K', 'm', 'locals', 'else##0')), (14, 12, 'Opaque', ('K', 'm', 'locals', 'else##0', '##raise')),
        (15, 8, 'For', ('K', 'm', 'locals', 'for i in rows or ()')), (16, 12, 'Opaque', ('K', 'm', 'locals', 'for i in rows or ()', '##continue')),
        (17, 8, 'Try', ('K', 'm', 'locals', 'try')), (18, 12, 'Expr', ('K', 'm', 'locals', 'try', 'a.b.c()')),
        (19, 8, 'except', ('K', 'm', 'locals', 'except (A, B) as e')), (20, 12, 'Opaque', ('K', 'm', 'locals', 'except (A, B) as e', '##pass')),
        (21, 8, 'Opaque', ('K', 'm', 'locals', '##while')), (21, 17, 'Expr', ('K', 'm', 'locals', '##while', '##statement')),
        (22, 0, 'If', ('if##0',)), (23, 4, 'Opaque', ('if##0', '##pass')),
    ]
    assert source.sites[9][:4] == (8, 0, 8, 8) and source.sites[10][:4] == (9, 4, 9, 16), 'a compound statement is its header, colon included'


def test_the_spans_the_live_value_overlays_read():
    """`live_view_views` reads four spans off a line's text: where its code ends (`_code_end_col`), the
    right-hand side of its assignment (`_rhs_span`), the name a value's box goes around (`_label_box_span`),
    and the `live_view(..)` call token. The source has them from its tokens: the same on every line that
    neither starts in a string nor holds a triple quote, except where the text scan read an operator or a
    name inside a string or inside brackets."""
    import re
    from meltygui.editor import live_view_views as views
    lines = ends = rights = wrong = labels = off = calls = 0
    # (editor_model.py is always among them: its calls are the ones compared with `ast` below.)
    for path in sorted({*sample(4), ROOT / 'meltygui/model/editor_model.py'}):
        text = path.read_text()
        rows = text.split('\n')
        source = Source(text)
        source.ask(now=True)
        assert source.land()
        states = list(source.native.states(0, len(rows)))
        for line, start, end, right in source.spans(0, len(rows)):
            if states[line] not in (0, 255) or '"""' in rows[line] or "'''" in rows[line] or not rows[line].isascii():
                continue
            lines += 1
            ends += end == views._code_end_col(rows[line])
            assert start == len(rows[line]) - len(rows[line].lstrip()) and end == views._code_end_col(rows[line]), (path, line)
            if right is not None:
                rights += 1
                wrong += views._rhs_span(rows[line], 0) != right
            names = set(re.findall(r'[A-Za-z_]\w*', rows[line][:end])) if line % 7 == 0 else ()
            for name in names:
                found = source.label(line, name)
                if found is not None:
                    labels += 1
                    off += tuple(views._label_box_span(name, rows[line])) != found
        if path.name == 'editor_model.py':
            tree = ast.parse(text)
            by_ast = {(node.lineno - 1, node.col_offset): (node.end_lineno - 1, node.end_col_offset) for node in ast.walk(tree) if isinstance(node, ast.Call)}
            for line, column, last, end in source.closes(0, len(rows)):
                calls += 1
                assert by_ast[(line, column)] == (last, end), (path, line, column)
    assert lines > 30000 and ends == lines and rights > 5000 and wrong * 50 <= rights and labels > 5000 and off * 50 <= labels and calls > 50, \
        (lines, ends, rights, wrong, labels, off, calls)
    print(f'{lines} lines end where the overlay reads it; {rights} right-hand sides, {wrong} read elsewhere; {labels} labels, {off} elsewhere; {calls} calls close where ast says')
    source = Source('value = compute(a, [b,\n    c])  # note = 1\nlive_view(value.shape)\nx: int = d["k=v"]\n')
    source.ask(now=True)
    assert source.land()
    assert source.spans(0, 9) == [(0, 0, 22, (8, 22)), (1, 4, 7, None), (2, 0, 22, None), (3, 0, 17, (9, 17))]
    assert source.label(2, 'value.shape') == (16, 21) and source.label(2, 'value') == (10, 15) and source.label(1, 'note') is None
    assert source.closes(0, 9) == [(0, 8, 1, 7), (2, 0, 2, 22)]


def legacy_runs(text, state):
    """The colour runs of one line by the legacy tokenizer (`_tokenize_from`), plain neighbours joined."""
    joined = ('default', 'comment', 'string', 'string_doc', 'keyword', 'operator_word')
    opener = None if state == 0 else (('"""', "'''")[(state - 1) % 2], ('string_doc', 'string', 'fstring')[(state - 1) // 2])
    found, column = [], 0
    for piece, kind in tools()._tokenize_from(text, opener):
        if found and found[-1][2] == kind and kind in joined:
            found[-1] = (found[-1][0], found[-1][1] + len(piece), kind)
        else:
            found.append((column, len(piece), kind))
        column += len(piece)
    return found


def test_colour_runs_and_string_states_agree_with_the_legacy_lexer_on_every_line_here():
    from meltygui.model.source_model import runs
    lines = 0
    for path in FILES:
        text = path.read_text()
        rows = text.split('\n')
        states = Source(text).native.states(0, len(rows))
        legacy = [None] + [state for _, state in tools()._iter_newline_states(text)]
        for line, row in enumerate(rows):
            assert runs(row, states[line]) == legacy_runs(row, states[line]), (path, line)
            # The legacy lexer let an ordinary string run over its line's end; no file here has one that does.
            assert legacy[line] is None or len(legacy[line][0]) == 1 or legacy_runs('', 0) == [] and LEGACY[states[line]] == legacy[line], (path, line)
            assert legacy[line] is not None or states[line] == 0, (path, line)
        lines += len(rows)
    assert lines > 100000


def test_colour_runs_agree_with_the_legacy_tokenizer_on_mangled_lines():
    from meltygui.model.source_model import runs
    random.seed(21)
    pieces = ['x', ' ', '"', "'", '"""', '#', '{', '}', 'f"', "rb'", '-', '1.5', '0', '(', ')', ', ', '=', 'def ', 'self', 'len', '@', '.', 'True',
              '\\', '!r', ':', '\uf054', 'é', '# [a=1, b=(0, 0.5, 1.0)]', "'#ff00aa'", '0x1F', '1e-3', ' and ', 'N{', '\t']
    rows = [row for row in SAMPLE.split('\n') if row.strip()]
    for _ in range(6000):
        row = random.choice(rows)
        for _ in range(random.randint(1, 4)):
            at = random.randint(0, len(row))
            row = row[:at] + random.choice(pieces) + row[at + random.choice([0, 0, 1, 5]):]
        for state in (0, random.randint(1, 6)):
            assert runs(row, state) == legacy_runs(row, state), (row, state)


def test_search_hits_agree_with_python():
    for text, term in [(SAMPLE, 'self'), (SAMPLE, 'SELF.DOC'), (SAMPLE, 'zzz'), ('aaaa\naAa', 'aa'),
                       ('Größe größe GRÖSSE\nÉcole école', 'É'), ('日本語 日本\n本', '本'), ('x', '')]:
        assert Source(text).native.read(term)[3] == hits(text, term) if term else Source(text).native.read(term)[3] == []


def landed(source, rows):
    """(ranges, names) of the fold ranges `rows` takes from what `source` landed."""
    rows.land(source.native, [], 0, 0)
    return rows.ranges(), rows.names()


def test_the_thread_reads_the_newest_text_and_wakes_by_handle():
    """The reading thread never runs Python: it wakes through a handle of the frame loop's wake queue
    (`_gui_native.Wakes`), which queues the token and rouses the event wait (here: a pipe)."""
    import os
    import select
    wakes = pytest.importorskip('meltygui.core.rendering._gui_native').Wakes()
    reader, writer = os.pipe()
    wakes.poke('fd', writer)
    woken = lambda wait=5: bool(select.select([reader], [], [], wait)[0]) and bool(os.read(reader, 64))
    source, rows = Source(SAMPLE, version=7), Rows(SAMPLE.count('\n') + 1, seeded=True)
    source.waker = (wakes.handle(), 3)
    assert not source.land()
    source.ask('self', rows=rows)
    assert woken() and wakes.take() == [3] and source.land() and not source.land()
    assert (source.version, source.term, source.stamp) == (7, 'self', 1)
    heads, spans, _, names, _, _ = oracle(SAMPLE)
    assert landed(source, rows) == ([(head, head + span) for head, span in zip(heads, spans)], names)
    assert source.hits.on(0, 10 ** 6) == hits(SAMPLE, 'self') and source.defs == source.read()[2] and len(source.defs) > 20
    # A burst of edits is read once, after it rests: the newest request wins.
    start = time.monotonic()
    for version in range(8, 20):
        source.splice(0, 0, '#\n', version)
        source.ask(rest=.2, rows=rows)
    assert woken() and time.monotonic() - start >= .2
    time.sleep(.05)
    assert not woken(0) and wakes.take() == [3] and source.land() and source.version == 19 and source.hits is None
    ranges, names = landed(source, rows)
    assert ranges[0] == (0, 11) and names[0] == ('comment', '#'), 'the twelve comment lines fold'
    # `settle` does not wait out the rest; `now` reads on the calling thread.
    source.splice(0, 0, 'x = 1\n', 20)
    source.ask(rest=30)
    source.settle()
    assert source.land() and source.version == 20
    source.splice(0, 0, 'y = 2\n', 21)
    source.ask(now=True)
    assert source.land() and source.version == 21 and source.stamp == 4
    # A wake needs no source and no interpreter to still be good: the handle keeps the queue.
    handle = wakes.handle()
    woken(0)                    # (the reading `settle` waited for woke too)
    del wakes
    source.waker = (handle, 9)
    source.splice(0, 0, 'z = 3\n')
    source.ask()
    assert woken() and source.land()
    with pytest.raises(TypeError):
        source.native.ask([(object(), 1)])
    os.close(reader), os.close(writer)


def test_anyone_splices_a_source_and_its_log_tells_what_happened():
    source = Source('def f():\n    pass\n', version=5)
    source.ask(now=True)
    assert source.land() and source.version == 5 == source.head and source.defs == [(False, 'f', 0, 1, 0, -1)]
    # A reading asked for before an edit still lands: it says which version it is of, and the log what came after.
    source.ask(rest=30)
    change = source.splice(0, 0, 'x = 1\n')
    assert change == (0, 0, 0, 0, 1, 0) and source.head == 6 and source.text == 'x = 1\ndef f():\n    pass\n'
    source.settle()
    assert source.land() and source.version == 5
    assert [entry[:5] for entry in source.since(5)] == [(6, change, 0, 0, 6)] and source.since(6) == [] and source.since(4) is None
    source.splice(0, 5, 'y')
    assert [(entry[0], entry[2], entry[3], entry[4]) for entry in source.since(5)] == [(6, 0, 0, 6), (7, 0, 5, 1)]
    source.ask(now=True)
    assert source.land() and source.version == 7 and source.defs == [(False, 'f', 1, 2, 0, -1)]
    for _ in range(1100):
        source.splice(0, 0, '#')
    assert source.since(7) is None and len(source.since(source.head - 512)) == 512, 'the log keeps the last splices'


def test_the_facts_carry_the_term_they_were_read_for():
    """Hits of a term asked for earlier never show as the hits of a later one."""
    source = Source('alpha beta\nbeta\n')
    source.ask('alpha', now=True)
    source.ask('beta', rest=30)
    assert source.land() and source.term == 'alpha' and source.hits.on(0, 9) == [(0, 0, 5)]
    source.settle()
    assert source.land() and source.term == 'beta' and source.hits.on(0, 9) == [(0, 6, 10), (1, 0, 4)]


def test_a_reading_thread_ends_on_close_and_starts_again():
    source = Source(SAMPLE)
    source.ask(rest=30)
    source.close()
    assert not source.land(), 'a request the thread had not started on is dropped'
    source.ask()
    source.settle()
    assert source.land(), 'the next request starts a thread again'


@pytest.mark.skipif(not Path('/proc/self/task').exists(), reason='threads are counted through /proc')
def test_dropping_a_source_ends_its_thread():
    def threads():
        found = 0
        for task in Path('/proc/self/task').iterdir():
            try:
                found += (task / 'comm').read_text().strip() == 'melty-source'
            except OSError:
                pass        # it ended while being counted
        return found

    source = Source(SAMPLE)
    source.ask()
    source.settle()     # (a request on its way holds the source through its wake)
    before = threads()
    del source
    deadline = time.monotonic() + 5
    while threads() >= before and time.monotonic() < deadline:
        time.sleep(.01)
    assert threads() < before


def test_the_attributes_read_per_statement_are_those_of_the_whole_text():
    """The worker reads the attributes live modules lack one top-level statement at a time
    (`editor_model.chains`, kept per statement, put together by `lacking`): the same findings as one
    reading of the whole text, which `attributes` is and which is compared with `check_source` above."""
    import sys
    import types
    from meltygui.model import editor_model

    def whole(text, module):
        return editor_model.lacking([(0, editor_model.chains(ast.parse(text)))], module)

    def each(text, module):
        source = Source(text)
        source.ask(now=True)
        assert source.land()
        return editor_model.lacking([(first, editor_model.chains(ast.parse(source.native.span(first, last))))
                                     for first, last, _ in source.native.tops()], module)

    text = ('import os\nimport os.path as osp\nfrom os import path\nimport json\n'
            'os.nothing_here\n'                                         # 5: told
            'def shadowed(os):\n    return os.nope\n'
            'def local():\n    import sys\n    return sys.nowhere_at_all\n'    # 10: told, the import is the statement's own
            'class Held:\n    size = osp.missing_one\n'                # 12: told
            'try:\n    os.guarded\nexcept AttributeError:\n    pass\n'
            'os.declared = 1\n'
            'def later():\n    return os.declared, path.nope_again\n'  # 19: told for `path`, not for what another statement declared
            'json = 1\njson.rebound_so_unknown\n')
    module = types.SimpleNamespace(os=__import__('os'), osp=__import__('os').path, path=__import__('os').path, sys=sys, json=1)
    module = type('module', (), vars(module))
    assert each(text, module) == whole(text, module) == [
        (5, "module 'os' has no attribute 'nothing_here'"), (10, "module 'sys' has no attribute 'nowhere_at_all'"),
        (12, "module 'posixpath' has no attribute 'missing_one'"), (19, "module 'posixpath' has no attribute 'nope_again'")]
    # Real files, against a running module that has every loaded top-level module under its name.
    running = type('module', (), {name: found for name, found in list(sys.modules.items()) if '.' not in name and found is not None})
    told = 0
    for path in sample(12):
        text = path.read_text()
        want = whole(text, running)
        assert each(text, running) == want, path
        told += len(want)
    assert told > 3, told
