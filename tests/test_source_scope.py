"""`Source(thing)` (model/source_scope_model.py): the code of a class, a function or a module as
its file on disk, beside `Source(text, path)`, which stays a text of its own."""
import functools
import importlib.util
import sys
from pathlib import Path

import pytest

from meltygui.model.source_file_model import File, flush, opened
from meltygui.model.source_model import Source
from meltygui.model.source_scope_model import Scope, where
from meltygui.model.tree_model import EMPTY, Branch, Code

TEXT = '''import functools

GAIN = 0.5


class Knobs:
    """doc"""
    gain: float = -0.25
    label = 'knob'

    class Inner:
        depth = 3

    def scale(self, x, factor=2.0):
        y = x * factor

        def inner(q=1):
            w = 2
            return w
        return y

    @property
    def level(self):
        low = 1
        return low


def wrapped(function):
    @functools.wraps(function)
    def wrapper(*values, **named):
        return function(*values, **named)
    return wrapper


@wrapped
def sample(my_name: str, loud=False):
    greeting = "Hello"
    print(f"{greeting}! {my_name}")


def twice():
    a = 1


def twice():
    a = 2


def make():
    def made(count=5):
        return count
    return made
'''


@pytest.fixture
def knobs(tmp_path, request):
    """A module imported from a temp folder: every write of these tests lands there."""
    path = tmp_path / 'knobs.py'
    path.write_text(TEXT)
    name = f'scope_knobs_{request.node.name}'
    spec = importlib.util.spec_from_file_location(name, path)
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    del sys.modules[name]


def test_source_of_a_text_is_what_it_was():
    source = Source('GAIN = 1\n', 'path.py')
    assert type(source) is Source and source.path == 'path.py' and source.text == 'GAIN = 1\n' and not source.fixed
    assert type(Source()) is Source and Source().text == '' and type(Source(None)) is Source
    assert type(Source(Code('a + b'))) is Source                    # any str is a text
    with pytest.raises(TypeError):
        Source('GAIN = 1\n', at='launch')                           # a text has no versions


def test_where_the_code_of_a_thing_is(knobs):
    path = knobs.__file__
    assert where(knobs) == (path, ())
    assert where(knobs.Knobs) == (path, ('Knobs',)) and where(knobs.Knobs.Inner) == (path, ('Knobs', 'Inner'))
    assert where(knobs.Knobs.scale) == where(knobs.Knobs().scale) == (path, ('Knobs', 'scale'))
    assert where(knobs.Knobs.level) == (path, ('Knobs', 'level'))               # a property: its getter
    assert where(knobs.sample) == (path, ('sample',))                           # the function a decorator wrapped
    assert where(knobs.make()) == (path, ('make', 'locals', 'made'))             # a function's own names are its locals
    assert where(Path(path)) == (path, ())
    for nothing in (len, 3, sys, functools.partial(print), knobs.wrapped(len)):
        with pytest.raises(TypeError, match='has code'):
            Source(nothing)


def test_the_tree_is_the_scope(knobs):
    cls, function, module = Source(knobs.Knobs), Source(knobs.sample), Source(knobs)
    assert type(cls) is Scope and isinstance(cls.file, File) and cls.file is function.file is module.file is opened(knobs.__file__)
    assert isinstance(cls.tree, Branch) and cls.tree.kind == 'class' and function.tree.kind == 'def' and module.tree.kind == 'module'
    assert list(module.tree) == ['GAIN', 'Knobs', 'wrapped', 'sample', 'twice', 'twice#1', 'make']
    assert Source(knobs.make()).tree['parameters']['count'] == 5
    assert list(cls.tree) == ['gain', 'label', 'Inner', 'scale', 'level'] and cls.tree['gain'] == -0.25
    assert function.tree.plain() == {'parameters': {'my_name': EMPTY, 'loud': False},
                                     'locals': {'greeting': 'Hello', 'print()': {0: 'f"{greeting}! {my_name}"'}}}
    assert Source(knobs.Knobs.Inner).tree.plain() == {'depth': 3}
    assert Source(knobs.Knobs.scale).tree['locals']['inner']['parameters']['q'] == 1
    assert Source(knobs.twice).tree['locals']['a'] == 2                         # the definition that stands
    assert cls.tree is cls.tree is Source(knobs.Knobs).tree is module.tree['Knobs']     # one object while the keys are the same


def test_a_write_is_in_the_file_and_in_every_scope(knobs):
    cls, module = Source(knobs.Knobs), Source(knobs)
    cls.tree['gain'] = 0.75
    Source(knobs.sample).tree['parameters']['loud'] = True
    Source(knobs.Knobs.Inner).tree['depth'] = 4
    assert module.tree['Knobs']['gain'] == 0.75 and cls.file.text == module.text
    flush()
    assert Path(knobs.__file__).read_text() == module.text == TEXT.replace('-0.25', '0.75').replace('loud=False', 'loud=True') \
        .replace('depth = 3', 'depth = 4')


def test_text_is_the_scope_and_setting_it_changes_the_file(knobs):
    cls, function, module = Source(knobs.Knobs), Source(knobs.sample), Source(knobs)
    assert module.text == TEXT and cls.text == TEXT[TEXT.index('class Knobs'):TEXT.index('\n\ndef wrapped')]
    assert function.text == 'def sample(my_name: str, loud=False):\n    greeting = "Hello"\n    print(f"{greeting}! {my_name}")\n'
    assert Source(knobs.Knobs.Inner).text == '    class Inner:\n        depth = 3\n'
    function.text = function.text.replace('"Hello"', '"Goodbye"')
    assert function.file.log[-1][2:5] == (TEXT.index('Hello'), 5, 7)            # what differs, not the function
    Source(knobs.Knobs.Inner).text = '    class Inner:\n        depth = 3\n        wide = True\n'
    module.text = module.text.replace('GAIN = 0.5', 'GAIN = 8')
    flush()
    expected = TEXT.replace('"Hello"', '"Goodbye"').replace('depth = 3\n', 'depth = 3\n        wide = True\n').replace('0.5', '8')
    assert Path(knobs.__file__).read_text() == module.text == expected
    module.file.settle()
    assert cls.tree['Inner'].plain() == {'depth': 3, 'wide': True} and module.tree['GAIN'] == 8
    assert function.tree['locals']['greeting'] == 'Goodbye'


def test_a_scope_whose_code_is_gone_is_empty_and_comes_back(knobs):
    cls, module = Source(knobs.Knobs), Source(knobs)
    whole = module.text
    module.text = whole.replace('class Knobs', 'class Dials')
    module.file.settle()
    assert len(cls.tree) == 0 and cls.tree.kind is None and cls.text == '' and 'Dials' in module.tree
    with pytest.raises(KeyError):
        cls.text = 'class Knobs:\n    pass\n'
    module.text = whole
    module.file.settle()
    assert cls.tree['gain'] == -0.25
    flush()
    assert Path(knobs.__file__).read_text() == TEXT


def test_a_statement_is_not_overwritten_by_a_value(knobs):
    module = Source(knobs)
    for branch, key in ((module.tree, 'Knobs'), (module.tree, 'sample'), (module.tree['sample'], 'parameters'),
                        (module.tree['sample'], 'locals'), (module.tree['Knobs'], 'Inner')):
        with pytest.raises(TypeError, match='never a statement'):
            branch[key] = 1
    flush()
    assert Path(knobs.__file__).read_text() == module.text == TEXT
