"""`File` (model/source_file_model.py, native/source/src/file.rs): a `Source` whose text is a file's.
Loading, writing behind every edit and following the changes of other programs."""
import os
import random
import time
from pathlib import Path

import pytest

from meltygui.model.source_file_model import File, flush, opened
from meltygui.model.source_model import Source

TEXT = '''GAIN = 0.5  # loudness
NAMES = ["a", "b", 'c']

class Knobs:
    gain: float = -0.25
    label = 'knob'
'''


@pytest.fixture
def path(tmp_path):
    found = tmp_path / 'knobs.py'
    found.write_text(TEXT)
    return found


def until(check, seconds=5.):
    """Wait for something another thread brings about; False when it does not come."""
    end = time.monotonic() + seconds
    while not check():
        if time.monotonic() > end:
            return False
        time.sleep(.002)
    return True


def follows(file, text):
    """Whether the text took `text` from its file (a change arrives when the text is asked for)."""
    return until(lambda: file.text == text)


def outside(path, text, rename):
    """Another program writes the file: in place, or to a temp name renamed over it."""
    if rename:
        other = path.with_name('other.tmp')
        other.write_bytes(text.encode())
        os.replace(other, path)
    else:
        path.write_bytes(text.encode())


def test_the_text_is_the_file(path):
    file = opened(path)
    assert isinstance(file, File) and isinstance(file, Source) and file.path == str(path)
    assert file.text == TEXT and file.head == 1 and file.native.lines == TEXT.count('\n') + 1
    assert file.tree.plain() == {'GAIN': 0.5, 'NAMES': ['a', 'b', 'c'], 'Knobs': {'gain': -0.25, 'label': 'knob'}}
    assert file.grown.version == file.head              # the io thread's reading: nothing was read here
    assert file.since(0)[0][1:5] == ((0, 0, 0, 0, TEXT.count('\n'), 0), 0, 0, len(TEXT))     # the load is a splice like any


def test_one_text_for_every_name_of_a_file(path, tmp_path, monkeypatch):
    link = tmp_path / 'link.py'
    link.symlink_to(path)
    monkeypatch.chdir(tmp_path)
    assert opened(path) is opened(str(path)) is opened(link) is opened('knobs.py')
    assert opened(tmp_path / 'other.py') is not opened(path)


def test_an_edit_is_in_the_file(path):
    os.chmod(path, 0o640)
    file = opened(path)
    file.tree['GAIN'] = 0.75
    file.tree['Knobs']['label'] = 'dial'
    file.splice(0, 0, '# top\n')
    flush()
    assert path.read_text() == file.text == '# top\n' + TEXT.replace('0.5 ', '0.75 ').replace("'knob'", "'dial'")
    file.text = file.text.replace('NAMES', 'WORDS')
    flush()
    assert 'WORDS = ["a", "b", \'c\']' in path.read_text() and path.read_text() == file.text
    assert os.listdir(path.parent) == ['knobs.py'] and os.stat(path).st_mode & 0o777 == 0o640
    # A write of its own is known by the file's stamp: it does not come back as a change.
    edits = len(file.log)
    time.sleep(.05)
    assert not file.arrive() and len(file.log) == edits and file.error is None


def test_line_endings_and_the_byte_order_mark_stay(tmp_path):
    path = tmp_path / 'dos.py'
    path.write_bytes(b'\xef\xbb\xbfA = 1\r\nB = "\xc3\xa9"\r\n')
    file = opened(path)
    assert file.text == 'A = 1\r\nB = "é"\r\n'
    file.tree['A'] = 22
    flush()
    assert path.read_bytes() == b'\xef\xbb\xbfA = 22\r\nB = "\xc3\xa9"\r\n'


@pytest.mark.parametrize('rename', [False, True])
def test_a_change_by_another_program_arrives(path, rename):
    file = opened(path)
    tree = file.tree
    changed = TEXT.replace('0.5', '0.125').replace("label = 'knob'", "label = 'knob'\n    extra = [1, 2]")
    outside(path, changed, rename)
    assert follows(file, changed)
    low, gone, came = file.log[-1][2:5]
    # One splice, from the first character that differs to the last: what both texts start and end with stays.
    assert (low, gone, came) == (9, len(TEXT) - 10, len(changed) - 10) and file.head == 2
    file.settle()
    assert file.tree['GAIN'] == 0.125 and file.tree['Knobs']['extra'].plain() == [1, 2]
    assert file.tree is not tree                        # other keys: another object, as after any edit
    # The text is the file's again, so the next edit is written over it.
    file.tree['GAIN'] = 2
    flush()
    assert path.read_text() == changed.replace('0.125', '2') == file.text


def test_two_sources_would_differ_one_file_does_not(path):
    """What the layer is for: two `Source(text, path)` of one file go apart, two handles on a `File` cannot."""
    first, second = Source(path.read_text(), str(path)), Source(path.read_text(), str(path))
    first.tree['GAIN'] = 1
    assert second.tree['GAIN'] == 0.5
    one, other = opened(path), opened(path)
    one.tree['GAIN'] = 1
    assert other.tree['GAIN'] == 1 and one is other
    first.close(), second.close()


def test_the_file_wins_over_an_edit_that_was_not_written(path):
    file = opened(path)
    file.tree
    theirs = TEXT.replace('knob', 'theirs')
    outside(path, theirs, True)
    file.splice(0, 0, '# mine\n')                       # made on a text the file has left behind
    assert follows(file, theirs)
    flush()
    assert path.read_text() == theirs
    file.settle()
    file.tree['GAIN'] = 9                               # made again, it is written
    flush()
    assert path.read_text() == theirs.replace('0.5 ', '9 ')


def test_a_file_that_is_not_there_is_made_by_the_first_edit(tmp_path):
    path = tmp_path / 'new.py'
    file = opened(path)
    assert file.text == '' and len(file.tree) == 0 and not path.exists()
    file.text = 'A = 1\n'
    flush()
    assert path.read_text() == 'A = 1\n'
    os.remove(path)                                     # deleted by someone: the next edit puts it back
    time.sleep(.02)
    file.splice(0, 0, 'B = 2\n')
    flush()
    assert path.read_text() == 'B = 2\nA = 1\n'


def test_a_file_that_is_no_utf8_is_shown_and_never_written(tmp_path, capsys):
    path = tmp_path / 'latin.py'
    path.write_bytes(b'NAME = "caf\xe9"\n')
    file = opened(path)
    assert file.text == 'NAME = "caf�"\n' and 'no UTF-8' in file.error and 'no UTF-8' in capsys.readouterr().err
    file.splice(0, 0, '# edit\n')
    flush()
    assert path.read_bytes() == b'NAME = "caf\xe9"\n'


def test_a_read_only_file_is_said_not_overwritten(path, capsys):
    os.chmod(path, 0o440)
    file = opened(path)
    file.tree['GAIN'] = 3
    flush()
    assert until(lambda: file.arrive() or file.error is not None)
    assert 'read-only' in file.error and path.read_text() == TEXT and str(path) in capsys.readouterr().err


def test_differ_names_what_changed():
    source = Source('alpha\nbéta\ngamma\n')
    assert source.native.differ('alpha\nbéta\ngamma\n') is None
    assert source.native.differ('alpha\nbêta\ngamma\n') == (7, 8, 'ê')
    assert source.native.differ('alpha\ngamma\n') == (6, 11, '')
    assert source.native.differ('') == (0, 17, '') and source.native.differ('x') == (0, 17, 'x')
    assert source.native.differ('alpha\nbéta\ngamma\nmore') == (17, 17, 'more')
    assert source.put('alpha\nbéta\ngamma\n') is None
    source.text = 'alpha\nbeta\ngamma\ndelta\n'
    assert source.text == 'alpha\nbeta\ngamma\ndelta\n' and source.head == 1 and source.log[-1][2:5] == (7, 8, 14)
    source.close()


WORDS = ('x', 'gain', ' = ', '0.5', '\n', '\n    ', 'é', '"text"', '[1, 2]', '# note', '(', ')', 'def f():', '\r\n', '𝒳')


@pytest.mark.parametrize('seed', range(4))
def test_bursts_of_edits_and_outside_writes_keep_file_and_text_equal(tmp_path, seed):
    rng = random.Random(seed)
    path = tmp_path / f'random{seed}.py'
    path.write_text(''.join(f'value{at} = {at}\n' for at in range(rng.choice((3, 80, 700)))))
    file = opened(path)
    model = path.read_text()                            # what the text has to be: the same edits on a str
    assert file.text == model

    def piece():
        return ''.join(rng.choice(WORDS) for _ in range(rng.randint(0, 6)))

    for round in range(40):
        if rng.random() < .3:
            # Another program rewrites the file: the text takes it.
            low = rng.randint(0, len(model))
            model = model[:low] + piece() + model[min(len(model), low + rng.randint(0, 40)):]
            outside(path, model, rng.random() < .5)
            assert follows(file, model), f'round {round}: the outside write did not arrive'
        else:
            # A burst of edits with no pause: they pile up behind the writer, the newest text wins.
            for _ in range(rng.randint(1, 60)):
                low = rng.randint(0, len(model))
                high = min(len(model), low + rng.choice((0, 0, 1, 3, 40, 3000)))
                new = piece()
                file.splice(low, high, new)
                model = model[:low] + new + model[high:]
            flush()
            assert file.text == model
            assert path.read_bytes().decode() == model, f'round {round}: the file is not the text'
    file.settle()
    assert file.tree is not None and file.native.differ(model) is None and os.listdir(tmp_path) == [path.name]


def test_the_compiled_copies_go_with_every_write(path):
    from importlib.util import cache_from_source
    copies = [Path(cache_from_source(str(path), optimization=level)) for level in ('', 1, 2)]
    copies[0].parent.mkdir()
    for copy in copies:
        copy.write_bytes(b'compiled')
    file = opened(path)
    assert file.text == TEXT and all(copy.exists() for copy in copies)       # reading removes nothing
    file.tree['GAIN'] = 0.6                                                   # the same size, maybe the same second
    flush()
    assert path.read_text() == TEXT.replace('0.5 ', '0.6 ') and not any(copy.exists() for copy in copies)
