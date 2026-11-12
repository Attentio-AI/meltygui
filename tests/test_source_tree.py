"""`Source.tree` (model/tree_model.py, native/source/src/tree.rs): the code as a mapping, read and written.

The differential tests take `ast` as the oracle on the repository's own files.
"""
import ast
from pathlib import Path

import pytest

from meltygui.model.source_model import Source
from meltygui.model.tree_model import EMPTY, Branch, Code, spell

ROOT = Path(__file__).resolve().parents[1]
TEXT = '''import os
GAIN = 0.5  # loudness
NAMES = ["a", "b", 'c']
SIZE = (1920, 1080)
OPTS = {"rate": 3e-4, 'deep': {"on": True, "none": None}, 7: -2}
THING = Thing(1, 2, mode="fast")
EXPR = GAIN * 2

class Knobs:
    """doc"""
    gain: float = -0.25
    label = 'knob'

    def scale(self, x, factor=2.0, *rest, flag: bool = False, **more):
        y = x * factor
        if x > 1:
            z = [1, 2,
                 3]
            return x * factor
        print("done", sep='')
        return x
'''


@pytest.fixture
def source():
    found = Source(TEXT)
    yield found
    found.close()


def settled(source):
    """The tree once everything asked for was read."""
    source.settle()
    return source.tree


def test_the_code_reads_as_keys_and_values(source):
    assert source.tree.plain() == {
        'GAIN': 0.5, 'NAMES': ['a', 'b', 'c'], 'SIZE': (1920, 1080),
        'OPTS': {'rate': 0.0003, 'deep': {'on': True, 'none': None}, 7: -2},
        'THING': {0: 1, 1: 2, 'mode': 'fast'}, 'EXPR': 'GAIN * 2',
        'Knobs': {'gain': -0.25, 'label': 'knob', 'scale': {
            'parameters': {'x': EMPTY, 'factor': 2.0, 'flag': False},
            'locals': {'y': 'x * factor', 'if##0': {'##if': 'x > 1', 'z': [1, 2, 3]}, 'print()': {0: 'done', 'sep': ''}}}}}


def test_a_branch_is_read_like_a_mapping(source):
    tree = source.tree
    knobs = tree['Knobs']
    assert isinstance(knobs, Branch) and tree['Knobs'] is knobs
    assert list(tree) == list(tree.keys()) == ['GAIN', 'NAMES', 'SIZE', 'OPTS', 'THING', 'EXPR', 'Knobs']
    assert len(tree) == 7 and 'GAIN' in tree and 'os' not in tree and tree.get('nope', 3) == 3
    keys = tree.keys()
    assert keys[1] == 'NAMES' and keys[-1] == 'Knobs' and keys.index('SIZE') == 2 and keys[1:3] == ['NAMES', 'SIZE']
    assert [kind for kind in (tree.kind, knobs.kind, knobs['scale'].kind, tree['NAMES'].kind, tree['THING'].kind)] == \
        ['module', 'class', 'def', 'list', 'call']
    assert type(tree['EXPR']) is Code and tree['NAMES'].text == '''["a", "b", 'c']''' and knobs.span == (8, 0, 8, 11)
    assert dict(tree['OPTS']['deep'].items()) == {'on': True, 'none': None}
    with pytest.raises(KeyError):
        tree['nope']
    with pytest.raises(KeyError):
        tree['Knobs']['scale']['parameters']['self']


def test_a_write_replaces_the_value_and_nothing_else(source):
    tree = source.tree
    tree['GAIN'] = 0.75
    tree['NAMES'][1] = 'bee "x"'
    tree['NAMES'][0] = "it's"
    tree['Knobs']['gain'] = 0.1 + 0.2
    tree['Knobs']['label'] = 'dial'
    tree['Knobs']['scale']['parameters']['x'] = 3
    tree['Knobs']['scale']['parameters']['x'] = 4
    tree['Knobs']['scale']['parameters']['flag'] = True
    tree['Knobs']['scale']['locals']['y'] = Code('x + factor')
    tree['Knobs']['scale']['locals']['if##0']['##if'] = Code('x >= 10')
    tree['Knobs']['scale']['locals']['if##0']['z'][2] = 33
    tree['OPTS']['deep']['on'] = False
    tree['OPTS'][7] = [1, (2.5,), {'k': None}]
    tree['THING']['mode'] = 'slow'
    tree['THING'][0] = 10
    assert tree['GAIN'] == 0.75 and tree['Knobs']['scale']['parameters']['x'] == 4      # before any of it was read again
    assert source.text == TEXT.replace('0.5 ', '0.75 ').replace('"a", "b"', '"it\'s", \'bee "x"\'').replace('-0.25', '0.3') \
        .replace("'knob'", "'dial'").replace('x, factor', 'x=4, factor').replace('= False', '= True').replace('y = x *', 'y = x +') \
        .replace('x > 1', 'x >= 10').replace('3]', '33]').replace('"on": True', '"on": False').replace('7: -2', "7: [1, (2.5,), {'k': None}]") \
        .replace('"fast"', '"slow"').replace('Thing(1,', 'Thing(10,')
    tree = settled(source)
    assert tree['OPTS'][7].plain() == [1, (2.5,), {'k': None}] and tree['Knobs']['gain'] == 0.3
    assert tree['Knobs']['scale']['parameters'].plain() == {'x': 4, 'factor': 2.0, 'flag': True}
    assert not source.grown.pending


def test_what_was_written_as_code_stays_code(source):
    tree = source.tree
    tree['EXPR'] = Code('3')
    assert settled(source)['EXPR'] == '3' and type(source.tree['EXPR']) is Code      # not the number it spells
    tree['EXPR'] = Code('[1, 2]')
    assert type(settled(source)['EXPR']) is Code and source.tree['EXPR'] == '[1, 2]'
    source.tree['EXPR'] = 4                                                            # written as a value: read as one
    assert settled(source)['EXPR'] == 4 and type(source.tree['EXPR']) is int


def test_spelling():
    assert spell(0.1 + 0.2) == '0.3' and spell(2.0) == '2.0' and spell(float('inf')) == "float('inf')" and spell(1e-7) == '1e-07'
    assert spell('a', '"x"') == '"a"' and spell('a', "'x'") == "'a'" and spell('say "a"', '"x"') == '\'say "a"\'' and spell('a\n') == "'a\\n'"
    assert spell(Code('a + b')) == 'a + b' and spell((1,)) == '(1,)' and spell({'a': [True, None]}) == "{'a': [True, None]}"
    assert spell(set()) == 'set()' and spell(3) == '3'


def test_writes_made_before_the_text_is_read_again():
    source = Source('a = 1\nb = [10, 20]\nc = "text"\n')
    tree, grown = source.tree, source.grown
    was = grown.stamp
    for at in range(2, 40):
        tree['a'] = at                              # a drag: each write lands on the one before it
        tree['b'][1] = at * 10
        assert tree['a'] == at and tree['b'][1] == at * 10
    assert source.text == 'a = 39\nb = [10, 390]\nc = "text"\n'
    # An edit by someone else before a value, and one inside it: the value is still found.
    source.splice(0, 0, '# note\n')
    source.splice(source.text.index('text') + 2, source.text.index('text') + 2, '--')
    tree['c'] = 'new'
    tree['b'][0] = 5
    assert source.text == '# note\na = 39\nb = [5, 390]\nc = "new"\n'
    # One that touches a value's end: its place is not known, so the text is read again first.
    source.splice(source.text.index('39'), source.text.index('39') + 2, '391')
    source.splice(source.text.index('391') + 3, source.text.index('391') + 3, '2')
    tree['a'] = 7
    assert source.text == '# note\na = 7\nb = [5, 390]\nc = "new"\n' and grown.stamp > was
    source.close()


def test_the_tree_follows_edits_of_the_text(source):
    tree = source.tree
    knobs, names = tree['Knobs'], tree['NAMES']
    source.splice(TEXT.index('0.5'), TEXT.index('0.5') + 3, '0.625')
    assert tree['GAIN'] == 0.5                      # as last read
    assert settled(source) is tree and tree['GAIN'] == 0.625            # the same keys: the same object
    source.splice(0, 0, 'FIRST = 1\n')
    fresh = settled(source)
    assert fresh is not tree and list(fresh)[:2] == ['FIRST', 'GAIN']    # other keys: another object
    # Branches held from before answer from the text as it is.
    assert knobs['label'] == 'knob' and names.plain() == ['a', 'b', 'c'] and list(tree)[0] == 'FIRST'
    source.splice(source.text.index('class Knobs'), len(source.text), '')
    settled(source)
    assert len(knobs) == 0 and knobs.kind is None and 'label' not in knobs
    with pytest.raises(KeyError):
        knobs['label']


def test_a_large_text_is_read_on_the_sources_thread():
    text = ''.join(f'value_{at} = {at}\n' for at in range(6000))
    source = Source(text)
    assert len(text) > 60000 and len(source.tree) == 0            # asked for, not read here
    tree = settled(source)
    assert len(tree) == 6000 and tree['value_4321'] == 4321 and tree.keys()[5999] == 'value_5999'
    tree['value_4321'] = -1
    assert source.native.texts(4321, 1) == ['value_4321 = -1']
    source.close()


def test_a_source_nobody_asked_the_tree_of_grows_none():
    source = Source(TEXT)
    source.ask(now=True)
    source.land()
    assert source.grown is None and source.native.tree_shape() is None
    assert len(source.tree) == 7 and source.native.tree_shape() is not None
    source.close()


def dump(branch):
    """The whole tree as nested lists, in order: what two readings of one text must agree on."""
    return [(key, dump(item) if isinstance(item, Branch) else (type(item).__name__, item)) for key, item in branch.items()]


def fresh(text):
    """The tree of `text` read from nothing, as `dump` gives it, and its shape."""
    source = Source(text)
    source.tree
    found = dump(settled(source)), source.native.tree_shape()[0]
    source.close()
    return found


def test_a_reading_after_an_edit_takes_the_parts_of_the_statements_that_did_not_change():
    text = ''.join(f'value_{at} = {at}\n\n\ndef function_{at}(a, b={at}):\n    return a + b\n\n\n' for at in range(3000))
    source = Source(text)
    source.tree
    tree = settled(source)
    _, entries, kept, grown = source.native.tree_shape()
    assert (kept, grown) == (0, 6000) and len(tree) == 6000 and entries == 3000 * 6      # a value; a def, its parameters (two) and locals
    tree['value_1500'] = -5                         # a value: its chunk's statements are read again, the rest is taken
    tree = settled(source)
    shape, _, kept, grown = source.native.tree_shape()
    assert kept + grown == 6000 and 0 < grown <= 40 and tree['value_1500'] == -5
    assert (dump(tree), shape) == fresh(source.text)
    source.close()


def test_keys_are_numbered_as_in_the_whole_text_after_edits():
    text = 'x = 1\nif a:\n    if b:\n        p = 1\nx = 2\nfor i in r:\n    q = i\nif c:\n    y = 3\nelse:\n    y = 4\nprint(1)\nprint(2)\nfor i in r:\n    q = 0\n'
    source = Source(text)
    tree = source.tree
    assert list(tree) == ['x', 'if##0', 'x#1', 'for i in r', 'if##2', 'else##0', 'print()', 'print()#1', 'for i in r##1']
    assert list(tree['if##0']) == ['##if', 'if##1'] and tree['x#1'] == 2
    source.splice(0, 0, 'x = 0\nif z:\n    w = 9\nprint(0)\n')      # every numbered key below moves on by one
    tree = settled(source)
    assert list(tree) == ['x', 'if##0', 'print()', 'x#1', 'if##1', 'x#2', 'for i in r', 'if##3', 'else##0', 'print()#1', 'print()#2', 'for i in r##1']
    assert list(tree['if##1']) == ['##if', 'if##2'] and [tree['x'], tree['x#1'], tree['x#2']] == [0, 1, 2]
    assert (dump(tree), source.native.tree_shape()[0]) == fresh(source.text)
    assert {tuple(key) for *_, key in source.sites} >= {('if##1', 'if##2', 'p'), ('if##3', 'y'), ('for i in r##1', 'q')}
    source.close()


def test_a_tree_read_after_edits_is_the_tree_of_the_text():
    import random
    rng = random.Random(7)
    text = (ROOT / 'meltygui/model/editor_model.py').read_text() + (ROOT / 'meltygui/view/dict_view.py').read_text()
    pool = ['x = 1\n', 'if x:\n    y = 2\n', 'def f(a=1):\n    return a\n', 'try:\n    pass\nexcept E:\n    pass\n', '"""\n', 'ITEMS = [1, 2,\n', ')\n']
    source = Source(text)
    source.tree
    settled(source)
    kept = 0
    for step in range(120):
        lines = source.text.split('\n')
        at, kind = rng.randrange(len(lines)), rng.randrange(4)
        start = sum(len(line) + 1 for line in lines[:at])
        if kind == 0:                                # some lines go
            count = rng.choice([1, 1, 2, 5, 40, 400])
            source.splice(start, min(start + sum(len(line) + 1 for line in lines[at:at + count]), len(source.text)), '')
        elif kind == 1:                              # a statement comes
            source.splice(start, start, rng.choice(pool))
        elif kind == 2:                              # some lines come again elsewhere
            copy = '\n'.join(lines[at:at + rng.choice([1, 3, 30])]) + '\n'
            source.splice(rng.randrange(len(source.text)), rng.randrange(len(source.text)) and start, copy)
        else:                                        # a character in a line
            source.splice(start + rng.randrange(len(lines[at]) + 1), start + len(lines[at]), rng.choice(['', ' ', '(', '"', '7']))
        tree = settled(source)
        shape = source.native.tree_shape()
        kept += shape[2]
        assert (dump(tree), shape[0]) == fresh(source.text), step
    assert kept > 120 * 20                           # (and it was not grown anew each time)
    source.close()


def test_a_large_deletion_is_followed():
    text = ''.join(f'value_{at} = {at}\n' for at in range(3000))
    source = Source(text)
    source.tree
    settled(source)
    low, high = text.index('value_500 '), text.index('value_2500 ')
    source.splice(low, high, '')                    # more statements than the tree before is looked through for one
    tree = settled(source)
    _, _, kept, grown = source.native.tree_shape()
    assert len(tree) == 1000 and tree.keys()[500] == 'value_2500' and tree['value_2999'] == 2999 and kept > 900 and grown < 80
    source.close()


def test_a_path_of_statements_is_their_site_key(source):
    tree = source.tree
    sites = {tuple(key) for *_, key in source.sites}

    def walk(branch):
        for key, item in branch.items():
            if isinstance(item, Branch):
                if item.kind in ('class', 'def', 'block'):
                    assert item.path in sites, item.path
                walk(item)
            elif branch.kind in ('module', 'class', 'locals', 'block') and not str(key).startswith('##'):
                assert branch.path + (key,) in sites, branch.path + (key,)
    walk(tree)


def plain(value):
    """What the tree answers, as `ast.literal_eval` would give it: code that is a literal is evaluated."""
    if isinstance(value, Branch):
        if value.kind == 'call':
            return ast.literal_eval(value.text)         # (`set()`, the one call that is a literal)
        kind, items = value.kind, [plain(item) for item in value.values()]
        return {'list': list, 'tuple': tuple, 'set': set}[kind](items) if kind in ('list', 'tuple', 'set') else dict(zip(value.keys(), items))
    # (In brackets: code that stood in some may go over several lines.)
    return ast.literal_eval(f'({value}\n)') if isinstance(value, Code) else value


def compare(body, branch, found):
    """Every name `body` assigns a literal once: the tree answers the same value."""
    names = [node.targets[0].id for node in body if isinstance(node, ast.Assign) and len(node.targets) == 1
             and isinstance(node.targets[0], ast.Name)]
    names += [node.target.id for node in body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value]
    names += [node.name for node in body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in body:
        if isinstance(node, ast.ClassDef) and names.count(node.name) == 1:
            compare(node.body, branch[node.name], found)
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else getattr(node, 'target', None)
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(target, ast.Name) or node.value is None or names.count(target.id) != 1:
            continue
        try:
            expected = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            assert isinstance(branch[target.id], (Code, Branch)), (target.id, branch[target.id])
            continue
        got = branch[target.id]
        assert plain(got) == expected and type(plain(got)) is type(expected), (target.id, got, expected)
        found[0] += 1
        found[1] += not isinstance(got, Code)


def test_values_agree_with_ast_on_the_repository():
    files = sorted((ROOT / 'meltygui').rglob('*.py'))
    found = [0, 0]
    for path in files:
        text = path.read_text()
        try:
            module = ast.parse(text)
        except SyntaxError:
            continue
        source = Source(text)
        source.tree                             # (a large one is read on its thread)
        compare(module.body, settled(source), found)
        source.close()
    # (Literals read as values, not left as code: all but implicit concatenations, bytes and the like.)
    assert found[0] > 1000 and found[1] > found[0] * .9, found
